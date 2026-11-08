"""多下载源的时延 + 速率探测：按路由智能择优。

客户端可以配置多个下载源（自建镜像），外加官方直连兜底。本模块对**同一份
资源**依次测各候选源的「首字节延迟」与「下载速率」，按综合评分选出更快的
一个，并把结论按路由缓存一段时间，避免每个文件都重新探测。

速率评分方式：``首字节延迟 + 1 MiB / 实测速率``，即「拿到 1 MiB 数据的预计
耗时」；速率拿不到时退化为只比延迟。这样低延迟但出口很窄的源不会因为
首字节快而胜出。

设计要点：

* 探测带 ``Range`` 限制，每个候选最多读 :data:`SPEED_BYTES`，成本可控；
* 探测前按冷却时间清理本机 DNS 缓存（Windows ``ipconfig /flushdns``），
  避免 DNS 污染把候选指向错误地址；
* 探测失败即视为该候选不可用，全部失败时回落到官方直连；
* 结论缓存默认 10 分钟，并由锁保护，并发下载时同一路由只会探测一次；
* 下载器发现速率过低时可通过 :meth:`SourceRouter.resolve_again` 强制重测，
  并把断点续传到更快的源。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from offcraft.mirror import HOST_ROUTES, DirectSource, Mirror, UnknownUpstreamError
from offcraft.net.downloader import DownloadError, Downloader
from offcraft.net.updater import flush_dns

__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "DEFAULT_TTL",
    "DNS_FLUSH_COOLDOWN",
    "Decision",
    "SourceRouter",
    "flush_dns",
    "flush_dns_cached",
    "make_source",
    "probe_source",
]

#: 探测结论的默认有效期（秒）
DEFAULT_TTL = 600.0
#: 单次探测的超时（秒）—— 宁可快失败，也不要卡住下载
DEFAULT_PROBE_TIMEOUT = 5.0
#: 延迟探测读取的字节数
PROBE_BYTES = 2048
#: 速率探测读取的字节数（每个候选每次采样）
SPEED_BYTES = 256 * 1024
#: 每个候选测量次数，取综合评分最好的一次以抵消抖动
PROBE_SAMPLES = 2
#: 速率评分参考字节数：把「传输 1 MiB 的耗时」计入总分
SPEED_REF_BYTES = 1024 * 1024
#: 两次 DNS 缓存清理之间的最短间隔（秒）
DNS_FLUSH_COOLDOWN = 30.0
#: 官方版本清单（直连探测地址）
_DIRECT_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

_flush_lock = threading.Lock()
_last_flush_at = 0.0


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value * 1000:.0f}ms"


def _fmt_rate(value: float | None) -> str:
    if not value or value <= 0:
        return "—"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB/s"
    return f"{value / 1024:.0f} KB/s"


def _score(latency: float | None, speed: float | None) -> float | None:
    """综合评分（越小越好）：拿不到速率时只比延迟。"""
    if latency is None:
        return None
    if speed and speed > 0:
        return latency + SPEED_REF_BYTES / speed
    return latency


def _normalize_bases(sources: Sequence[str]) -> list[str]:
    """去空白、去重并丢弃非法地址；空串表示官方直连。"""
    clean: list[str] = []
    for item in sources:
        text = str(item or "").strip().rstrip("/")
        if text:
            try:
                Mirror(text)
            except ValueError:
                continue
        if text not in clean:
            clean.append(text)
    return clean or [""]


def flush_dns_cached(*, cooldown: float = DNS_FLUSH_COOLDOWN, force: bool = False) -> bool:
    """带冷却的 DNS 缓存清理；冷却期内直接跳过，返回是否真正执行。"""
    global _last_flush_at
    now = time.monotonic()
    with _flush_lock:
        if not force and now - _last_flush_at < cooldown:
            return False
        _last_flush_at = now
    try:
        return bool(flush_dns())
    except Exception:  # noqa: BLE001 - 清理缓存失败不应影响探测
        return False


@dataclass(slots=True)
class Decision:
    """一次择优的结论。"""

    key: str
    mode: str  # "mirror" / "direct"
    mirror_sec: float | None = None
    direct_sec: float | None = None
    #: 被选中的源基地址（空串 = 官方直连）
    source: str = ""
    #: 被选中的下载地址
    url: str = ""
    #: 被选中源的实测速率（字节/秒）
    speed: float | None = None
    #: 镜像 / 直连的实测速率（兼容旧展示）
    mirror_speed: float | None = None
    direct_speed: float | None = None
    at: float = 0.0
    reason: str = ""
    #: 多源候选明细（每个候选的延迟 / 速率）
    candidates: tuple[dict[str, object], ...] = ()
    #: 多源择优时的完整描述（为空时用旧版「镜像/直连」格式）
    detail_text: str = ""

    @property
    def label(self) -> str:
        return "镜像" if self.mode == "mirror" else "直连"

    @property
    def detail(self) -> str:
        if self.detail_text:
            return self.detail_text
        return (
            f"镜像 {_fmt_ms(self.mirror_sec)} / 直连 {_fmt_ms(self.direct_sec)}"
            f" → {self.label}"
            + (f"（{self.reason}）" if self.reason else "")
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "mode": self.mode,
            "source": self.source,
            "mirror_ms": None if self.mirror_sec is None else round(self.mirror_sec * 1000, 1),
            "direct_ms": None if self.direct_sec is None else round(self.direct_sec * 1000, 1),
            "speed_bps": None if self.speed is None else round(self.speed, 1),
            "detail": self.detail,
        }


class SourceRouter:
    """在多个下载源与官方直连之间按「延迟 + 速率」择优。

    接口与 :class:`offcraft.mirror.Mirror` 兼容（``rewrite`` / ``rewrite_optional`` /
    ``is_mirror_url`` / ``base_url`` / ``api_url``），可以直接当作 ``mirror``
    参数传给安装计划等。

    两种构造方式：

    * ``SourceRouter(Mirror(base), ...)``：旧版单镜像 + 直连二选一；
    * ``SourceRouter(sources=[...], ...)``：多源列表（空串表示官方直连）。
    """

    def __init__(
        self,
        mirror: Mirror | None = None,
        *,
        sources: Sequence[str] | None = None,
        downloader: Downloader | None = None,
        proxy: str | None = None,
        token: str | None = None,
        ttl: float = DEFAULT_TTL,
        timeout: float = DEFAULT_PROBE_TIMEOUT,
        samples: int = PROBE_SAMPLES,
        speed_bytes: int = SPEED_BYTES,
        enabled: bool = True,
        flush: bool = True,
        flush_cooldown: float = DNS_FLUSH_COOLDOWN,
    ) -> None:
        self._legacy = sources is None
        if self._legacy:
            if mirror is None:
                raise ValueError("SourceRouter 需要 mirror 或 sources")
            self.bases = [mirror.base_url, ""]
        else:
            self.bases = _normalize_bases(sources or [])
        self.mirror = mirror if self._legacy else None
        self.ttl = max(0.0, float(ttl))
        self.timeout = max(0.5, float(timeout))
        self.samples = max(1, int(samples))
        self.speed_bytes = max(1024, int(speed_bytes))
        self.enabled = bool(enabled)
        self.flush_enabled = bool(flush)
        self.flush_cooldown = max(0.0, float(flush_cooldown))
        self.rescan_cooldown = 10.0
        primary = self._primary_base()
        self._prober = downloader or Downloader(
            proxy=proxy or None,
            token=token or None,
            token_origin=(primary or (mirror.base_url if mirror else None)),
            workers=1,
            timeout=self.timeout,
            retries=0,
        )
        self._lock = threading.RLock()
        self._cache: dict[str, Decision] = {}
        #: 已下发的下载地址 → 官方原始地址，用于过慢时重新改写
        self._reverse: dict[str, str] = {}
        self._last_rescan = 0.0

    # ------------------------------------------------------------- 镜像接口
    def _primary_base(self) -> str:
        for base in self.bases:
            if base:
                return base
        return ""

    @property
    def base_url(self) -> str:
        return self._primary_base()

    def api_url(self, path: str) -> str:
        """镜像自有 API（皮肤/房间/看板）不参与择优，固定走首个镜像源。"""
        if self.mirror is not None:
            return self.mirror.api_url(path)
        base = self.base_url
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}" if base else path

    def is_mirror_url(self, url: str) -> bool:
        if self.mirror is not None:
            return self.mirror.is_mirror_url(url)
        return any(
            base and url.startswith(base + "/") for base in self.bases
        )

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.base_url or "直连"

    def rewrite(self, url: str) -> str:
        """返回该资源应该使用的地址（择优后的下载源或官方地址）。"""
        if self._legacy:
            target = self._mirrored(url)
            if target is None:
                return url
            mirrored, key = target
            if not self.enabled:
                return mirrored
            decision = self.decide(key, mirrored, url)
            chosen = mirrored if decision.mode == "mirror" else url
            self._remember(url, chosen)
            return chosen
        return self._rewrite_multi(url)

    def rewrite_optional(self, url: str | None) -> str | None:
        return None if url is None else self.rewrite(url)

    # ------------------------------------------------------------- 多源改写
    def _candidates(self, url: str) -> list[tuple[str, str]]:
        """``url`` → ``[(源基地址, 候选下载地址), ...]``（空串 = 直连）。"""
        host = (urlsplit(url).hostname or "").lower()
        if host not in HOST_ROUTES:
            return [("", url)]
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for base in self.bases:
            if not base:
                pair = ("", url)
            else:
                try:
                    pair = (base, Mirror(base).rewrite(url))
                except (ValueError, UnknownUpstreamError):
                    continue
            if pair[1] not in seen:
                seen.add(pair[1])
                out.append(pair)
        return out or [("", url)]

    def _rewrite_multi(self, url: str) -> str:
        candidates = self._candidates(url)
        if not candidates:
            return url
        if not self.enabled:
            first_mirror = next((item for item in candidates if item[0]), None)
            return first_mirror[1] if first_mirror is not None else url
        key = self._route_key(url)
        decision = self.decide_multi(key, candidates)
        chosen = decision.url or candidates[0][1]
        self._remember(url, chosen)
        return chosen

    def _route_key(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        return f"route:{HOST_ROUTES.get(host, host)}"

    def _mirrored(self, url: str) -> tuple[str, str] | None:
        """旧版单镜像路径：``url`` → (镜像地址, 路由键)；无法映射时返回 None。"""
        if self.mirror is None:
            return None
        try:
            mirrored = self.mirror.rewrite(url)
        except UnknownUpstreamError:
            return None
        host = (urlsplit(url).hostname or "").lower()
        return mirrored, f"route:{HOST_ROUTES.get(host, host)}"

    def _remember(self, official: str, chosen: str) -> None:
        with self._lock:
            if len(self._reverse) > 8192:
                self._reverse.clear()
            self._reverse[chosen] = official

    # ------------------------------------------------------------- 探测
    def measure_stats(self, url: str) -> dict[str, float | None] | None:
        """测一次（延迟 + 速率），失败返回 None；按综合评分取最好的一次。"""
        best: dict[str, float | None] | None = None
        best_score: float | None = None
        for _ in range(self.samples):
            try:
                prober = self._prober
                if hasattr(prober, "probe_sample"):
                    stats = prober.probe_sample(
                        url, sample_bytes=self.speed_bytes, timeout=self.timeout
                    )
                else:  # 兼容只提供 probe() 的旧下载器
                    elapsed = prober.probe(url, first_chunk=PROBE_BYTES, timeout=self.timeout)
                    stats = {"ttfb": elapsed, "speed": None, "bytes": PROBE_BYTES}
            except (DownloadError, OSError):
                break
            ttfb = stats.get("ttfb")
            if ttfb is None:
                break
            score = _score(ttfb, stats.get("speed"))
            if best is None or (score is not None and (best_score is None or score < best_score)):
                best = stats
                best_score = score
        return best

    def measure(self, url: str) -> float | None:
        """测一次首字节耗时（秒）；失败返回 None。"""
        stats = self.measure_stats(url)
        return None if stats is None else stats.get("ttfb")

    def _flush(self, *, force: bool = False) -> bool:
        if not self.flush_enabled:
            return False
        return flush_dns_cached(cooldown=self.flush_cooldown, force=force)

    def _cached(self, key: str) -> Decision | None:
        """返回仍在有效期内的结论；``ttl=0`` 表示不缓存（每次都重新探测）。"""
        with self._lock:
            decision = self._cache.get(key)
        if decision is None or self.ttl <= 0:
            return None
        if (time.monotonic() - decision.at) < self.ttl:
            return decision
        return None

    def decide(self, key: str, mirror_url: str | None, direct_url: str | None) -> Decision:
        """旧版二选一：比较镜像与直连并缓存结论。"""
        cached = self._cached(key)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(key)
            if (
                cached is not None
                and self.ttl > 0
                and (time.monotonic() - cached.at) < self.ttl
            ):
                return cached
            self._flush()
            mirror_stats = self.measure_stats(mirror_url) if mirror_url else None
            direct_stats = self.measure_stats(direct_url) if direct_url else None
            mirror_sec = None if mirror_stats is None else mirror_stats.get("ttfb")
            direct_sec = None if direct_stats is None else direct_stats.get("ttfb")
            mirror_speed = None if mirror_stats is None else mirror_stats.get("speed")
            direct_speed = None if direct_stats is None else direct_stats.get("speed")
            mirror_score = _score(mirror_sec, mirror_speed)
            direct_score = _score(direct_sec, direct_speed)
            reason = ""
            if mirror_url is None:
                mode, reason = "direct", "无镜像映射"
            elif mirror_sec is None and direct_sec is None:
                mode, reason = "direct", "两者均不可达"
            elif mirror_sec is None:
                mode, reason = "direct", "镜像不可用"
            elif direct_sec is None:
                mode, reason = "mirror", "直连不可达"
            elif mirror_score is not None and direct_score is not None and mirror_score <= direct_score:
                mode, reason = "mirror", ""
            else:
                mode, reason = "direct", "直连更快"
            chosen_url = mirror_url if mode == "mirror" and mirror_url else (direct_url or "")
            decision = Decision(
                key=key,
                mode=mode,
                mirror_sec=mirror_sec,
                direct_sec=direct_sec,
                source=self._primary_base() if mode == "mirror" else "",
                url=chosen_url,
                speed=mirror_speed if mode == "mirror" else direct_speed,
                mirror_speed=mirror_speed,
                direct_speed=direct_speed,
                at=time.monotonic(),
                reason=reason,
            )
            self._cache[key] = decision
            return decision

    def decide_multi(
        self, key: str, candidates: Iterable[tuple[str, str]]
    ) -> Decision:
        """多源择优：按综合评分排序并缓存。"""
        items = list(candidates)
        cached = self._cached(key)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(key)
            if (
                cached is not None
                and self.ttl > 0
                and (time.monotonic() - cached.at) < self.ttl
            ):
                return cached
            self._flush()
            rows: list[dict[str, object]] = []
            for base, url in items:
                stats = self.measure_stats(url)
                latency = None if stats is None else stats.get("ttfb")
                speed = None if stats is None else stats.get("speed")
                rows.append(
                    {
                        "base": base,
                        "url": url,
                        "latency": latency,
                        "speed": speed,
                        "score": _score(latency, speed),
                    }
                )
            reachable = [row for row in rows if row["latency"] is not None]
            if reachable:
                best = min(reachable, key=lambda row: (row["score"], rows.index(row)))
            else:
                best = next((row for row in rows if not row["base"]), rows[0])
            chosen_base = str(best["base"])
            mode = "mirror" if chosen_base else "direct"
            reason = ""
            if not reachable:
                reason = "所有源均不可达"
            elif mode == "direct" and any(row["base"] and row["latency"] is not None for row in rows):
                reason = "直连更快"
            mirror_rows = [row for row in rows if row["base"]]
            chosen_mirror = next(
                (row for row in reachable if row["base"]), None
            )
            direct_row = next((row for row in rows if not row["base"]), None)
            parts: list[str] = []
            for row in rows:
                name = str(row["base"]) or "直连"
                text = f"{name} {_fmt_ms(row['latency'])}"
                if row["speed"]:
                    text += f" {_fmt_rate(row['speed'])}"
                parts.append(("★ " if row is best else "") + text)
            detail_text = " / ".join(parts) + f" → {chosen_base or '直连'}"
            decision = Decision(
                key=key,
                mode=mode,
                mirror_sec=None if chosen_mirror is None else chosen_mirror["latency"],
                direct_sec=None if direct_row is None else direct_row["latency"],
                source=chosen_base,
                url=str(best["url"]),
                speed=None if best["speed"] is None else float(best["speed"]),
                mirror_speed=None if chosen_mirror is None else chosen_mirror["speed"],
                direct_speed=None if direct_row is None else direct_row["speed"],
                at=time.monotonic(),
                reason=reason,
                candidates=tuple(rows),
                detail_text=detail_text,
            )
            self._cache[key] = decision
            return decision

    def rank(
        self, candidates: Iterable[tuple[str, str]], key: str, probe_path: str = "/"
    ) -> list[tuple[str, str]]:
        """把 ``[(源名, 基地址), ...]`` 按延迟 + 速率综合评分排序（不可达的排最后）。

        各候选的探测地址为 ``基地址 + probe_path``，因此不同源的探测指向
        同一类资源；结论同样缓存。
        """
        items = list(candidates)
        if len(items) < 2 or not self.enabled:
            return items
        measured: list[tuple[float | None, int, tuple[str, str]]] = []
        self._flush()
        for index, (name, base) in enumerate(items):
            cache_key = f"{key}:{name}"
            cached = self._cached(cache_key)
            if cached is not None:
                score = _score(cached.direct_sec, cached.speed)
            else:
                stats = self.measure_stats(base.rstrip("/") + probe_path)
                latency = None if stats is None else stats.get("ttfb")
                speed = None if stats is None else stats.get("speed")
                score = _score(latency, speed)
                with self._lock:
                    self._cache[cache_key] = Decision(
                        key=cache_key,
                        mode="mirror" if name == "mirror" else "direct",
                        direct_sec=latency,
                        speed=speed,
                        at=time.monotonic(),
                    )
            measured.append((score, index, (name, base)))
        measured.sort(
            key=lambda item: (
                item[0] is None,
                item[0] if item[0] is not None else 0.0,
                item[1],
            )
        )
        return [item[2] for item in measured]

    # ------------------------------------------------------------- 慢速重测
    def resolve_again(self, url: str) -> str | None:
        """下载过慢时重新探测：返回新的下载地址；没有更优地址时返回 None。"""
        with self._lock:
            official = self._reverse.get(url)
        if official is None:
            return None
        now = time.monotonic()
        with self._lock:
            if now - self._last_rescan >= self.rescan_cooldown:
                self._cache.clear()
                self._last_rescan = now
                reset = True
            else:
                reset = False
        if reset:
            self._flush(force=True)
        new_url = self.rewrite(official)
        return new_url if new_url and new_url != url else None

    def force_rescan(self) -> None:
        """清空结论并清理 DNS 缓存，下一次改写会重新探测全部候选。"""
        with self._lock:
            self._cache.clear()
            self._last_rescan = time.monotonic()
        self._flush(force=True)

    # ------------------------------------------------------------- 观测
    def decisions(self) -> list[dict[str, object]]:
        with self._lock:
            return [item.as_dict() for item in self._cache.values()]

    def summary(self) -> str:
        """形如 ``libs: 镜像 12ms / 直连 240ms → 镜像`` 的一行描述。"""
        rows = self.decisions()
        if not rows:
            return "尚未探测"
        return "；".join(f"{row['key']}: {row['detail']}" for row in rows)

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()
            self._reverse.clear()


def make_source(
    base: str = "",
    *,
    sources: Sequence[str] | None = None,
    downloader: Downloader | None = None,
    proxy: str = "",
    token: str = "",
    enabled: bool = True,
    **kwargs: object,
) -> Mirror | DirectSource | SourceRouter:
    """按配置构造下载源。

    * ``sources`` 给定时使用多源列表（空串表示官方直连）；
    * 否则沿用旧版单地址：空地址走直连，其余交给 :class:`SourceRouter` 择优。
    """
    if sources is not None:
        clean = _normalize_bases(sources)
        mirrors = [item for item in clean if item]
        if not mirrors:
            return DirectSource()
        if not enabled:
            return Mirror(mirrors[0])
        return SourceRouter(
            sources=clean,
            downloader=downloader,
            proxy=proxy,
            token=token,
            enabled=enabled,
            **kwargs,  # type: ignore[arg-type]
        )
    base = (base or "").strip().rstrip("/")
    if not base:
        return DirectSource()
    try:
        mirror = Mirror(base)
    except ValueError:
        return DirectSource()
    if not enabled:
        return mirror
    return SourceRouter(  # type: ignore[arg-type]
        mirror, downloader=downloader, proxy=proxy, token=token, **kwargs
    )


def probe_source(
    base: str,
    *,
    proxy: str = "",
    token: str = "",
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    sample_bytes: int = SPEED_BYTES,
    downloader: Downloader | None = None,
) -> dict[str, object]:
    """测单个下载源的延迟与速率（供 F12 对话框展示）。

    ``base`` 为空表示官方直连；探测对象固定为版本清单，既能验证路由，
    也能拿到有代表性的小文件速率。
    """
    base = (base or "").strip().rstrip("/")
    url = f"{base}/meta/mc/game/version_manifest_v2.json" if base else _DIRECT_MANIFEST
    prober = downloader or Downloader(
        proxy=proxy or None,
        token=token or None,
        token_origin=base or None,
        workers=1,
        timeout=timeout,
        retries=0,
    )
    flush_dns_cached()
    try:
        if hasattr(prober, "probe_sample"):
            stats = prober.probe_sample(url, sample_bytes=sample_bytes, timeout=timeout)
        else:
            elapsed = prober.probe(url, first_chunk=PROBE_BYTES, timeout=timeout)
            stats = {"ttfb": elapsed, "speed": None, "bytes": PROBE_BYTES}
    except (DownloadError, OSError) as exc:
        return {"base": base, "url": url, "ok": False, "error": str(exc)}
    if stats.get("ttfb") is None:
        return {"base": base, "url": url, "ok": False, "error": "空响应"}
    return {
        "base": base,
        "url": url,
        "ok": True,
        "latency": stats.get("ttfb"),
        "speed": stats.get("speed"),
        "bytes": stats.get("bytes"),
    }
