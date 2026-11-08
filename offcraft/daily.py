"""每日一言：从公开 API 取一条句子，并按自然日缓存到数据目录。

接口（https://dlystc.unknownmp.top）：

- ``/api/v2/sentence``：JSON，字段 ``content`` / ``source`` / ``author``
- ``/api/v2/sentence/text``：纯文本
- ``/api/v2/sentence/text?format=full``：纯文本，附带作品与作者

界面只读取 :func:`today_sentence`：先命中当天缓存，否则后台拉取并写回缓存；
网络失败时退回内置句子，保证启动页永远有一行字可显示。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "API_FULL",
    "API_JSON",
    "API_TEXT",
    "FALLBACK",
    "Sentence",
    "cache_path",
    "fetch_sentence",
    "load_cached",
    "parse_payload",
    "store_cached",
    "today_sentence",
]

API_JSON = "https://dlystc.unknownmp.top/api/v2/sentence"
API_TEXT = "https://dlystc.unknownmp.top/api/v2/sentence/text"
API_FULL = "https://dlystc.unknownmp.top/api/v2/sentence/text?format=full"

FALLBACK_TEXT = "愿今天的每一次启动都顺利。"


@dataclass(frozen=True)
class Sentence:
    """一句话及其出处；``author`` / ``work`` 可能为空。"""

    text: str
    author: str = ""
    work: str = ""

    @property
    def display(self) -> str:
        """带出处的展示文本（作者在前、作品在后，与 full 接口一致）。"""
        text = self.text.strip() or FALLBACK_TEXT
        tail = ""
        if self.author and self.work:
            tail = f"{self.author}《{self.work}》"
        elif self.author:
            tail = self.author
        elif self.work:
            tail = f"《{self.work}》"
        return f"{text} —— {tail}" if tail else text

    @property
    def byline(self) -> str:
        if self.author and self.work:
            return f"{self.author}《{self.work}》"
        return self.author or (f"《{self.work}》" if self.work else "")


FALLBACK = Sentence(FALLBACK_TEXT)


def parse_payload(payload: Any) -> Sentence:
    """把 JSON 对象或纯文本响应解析成 :class:`Sentence`。

    JSON 里 ``source`` 是作品名、``author`` 是作者；纯文本按空格切分不可靠，
    所以只把它当作一句话本身（已经带出处的 full 文本也原样保留）。
    """
    if isinstance(payload, dict):
        content = str(payload.get("content") or payload.get("text") or "").strip()
        author = str(payload.get("author") or "").strip()
        work = str(payload.get("work") or payload.get("source") or "").strip()
        return Sentence(content or FALLBACK_TEXT, author, work)
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        text = payload.strip()
        # full 文本形如「句子 作者 作品」：只做最保守的右侧两段拆解
        parts = text.rsplit(" ", 2)
        if len(parts) == 3 and parts[0]:
            return Sentence(parts[0].strip(), parts[1].strip(), parts[2].strip())
        return Sentence(text or FALLBACK_TEXT)
    return FALLBACK


def fetch_sentence(downloader: Any) -> Sentence:
    """优先 JSON 接口（字段可靠），失败后回退到 full 纯文本。"""
    try:
        return parse_payload(downloader.fetch_json(API_JSON))
    except Exception:  # noqa: BLE001 - 任何网络/解析失败都走文本兜底
        pass
    try:
        raw = downloader.fetch_bytes(API_FULL, max_size=8 * 1024)
        return parse_payload(raw)
    except Exception:  # noqa: BLE001
        return parse_payload(downloader.fetch_bytes(API_TEXT, max_size=8 * 1024))


def cache_path(data_dir: str | Path, day: str | None = None) -> Path:
    day = day or time.strftime("%Y-%m-%d")
    return Path(data_dir) / "daily" / f"{day}.json"


def load_cached(data_dir: str | Path, day: str | None = None) -> Sentence | None:
    path = cache_path(data_dir, day)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    sentence = parse_payload(data)
    return sentence if sentence.text.strip() else None


def store_cached(data_dir: str | Path, sentence: Sentence, day: str | None = None) -> Path:
    path = cache_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"text": sentence.text, "author": sentence.author, "work": sentence.work}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def today_sentence(data_dir: str | Path, downloader: Any | None = None) -> Sentence:
    """当天缓存优先；没有缓存且给了 downloader 就拉取并写回。"""
    cached = load_cached(data_dir)
    if cached is not None:
        return cached
    if downloader is None:
        return FALLBACK
    try:
        sentence = fetch_sentence(downloader)
    except Exception:  # noqa: BLE001
        return FALLBACK
    if not sentence.text.strip():
        return FALLBACK
    try:
        store_cached(data_dir, sentence)
    except OSError:
        pass
    return sentence
