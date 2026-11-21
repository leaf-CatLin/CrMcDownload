"""版本号工具：归一化、比较与从 Git tag 里取版本。

单独成模块是为了避免循环依赖：``update.py``（对外负载）与 ``releases.py``
（GitHub 自动检测 + 缓存）都要比较版本，二者都从这里导入。

排序规则（从小到大）：

1. 预发布：``alpha``/``a`` < ``beta``/``b`` < ``rc``/``pre``，数字后缀参与比较；
2. 正式版：``0.9.1``；
3. 重发版：``0.9.1-r1`` / ``-rev2``（同一版本的修订重发，排在正式版之后）。

``v`` 前缀与 ``+build`` 元数据忽略；非法版本返回 ``None``（调用方一律按「无法
比较」放行，不因为脏数据把用户挡在外面）。
"""

from __future__ import annotations

import re

__all__ = [
    "is_outdated",
    "normalize",
    "tag_version",
    "version_key",
]

_VERSION = re.compile(r"^[0-9]+(\.[0-9]+){0,3}$")
#: 版本号首次出现的形态，用于从 ``v0.8.5-r1`` / ``release-0.9.1`` 里抠版本
_TAG_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?")
#: 预发布标签 → 排序档位（越小越旧）
_PRERELEASE_RANK = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2, "pre": 2}
#: 重发标签 → 排在正式版之后
_REVISION_LABELS = {"r", "rev", "revision", "hotfix", "fix"}


def normalize(text: str | None) -> str:
    """去掉 ``v`` 前缀与首尾空白，截断到 32 字符。"""
    return str(text or "").strip().lstrip("vV")[:32]


def tag_version(tag: str | None) -> str:
    """从 Git tag 里取版本号：``v0.8.5-r1`` → ``0.8.5-r1``，取不到就原样归一化。"""
    raw = normalize(tag)
    if not raw:
        return ""
    match = _TAG_VERSION.search(raw)
    return match.group(0) if match else raw


def version_key(text: str | None) -> tuple[tuple[int, ...], tuple[int, tuple[int, ...]]] | None:
    """把版本号归一化成可比较的键；非法版本返回 ``None``。

    ``0.7.0-beta.1`` 排在 ``0.7.0`` 之前，``0.8.5-r1`` 排在 ``0.8.5`` 之后。
    """
    raw = normalize(text)
    if not raw:
        return None
    raw = raw.partition("+")[0]
    base, _, pre = raw.partition("-")
    if not _VERSION.match(base):
        return None
    numbers = [int(part) for part in base.split(".")]
    while len(numbers) < 3:
        numbers.append(0)
    tail = tuple(numbers)
    if not pre:
        return tail, (3, ())
    chunks = [chunk for chunk in re.split(r"[.\-_]", pre.lower()) if chunk]
    if not chunks:
        return tail, (3, ())
    head, rest = chunks[0], chunks[1:]
    label = re.sub(r"[0-9]+$", "", head)
    digits = [int(part) for part in rest if part.isdigit()]
    if not label and head.isdigit():
        # ``0.8.5-1``：当作重发序号
        return tail, (4, (int(head),))
    if label in _REVISION_LABELS:
        inline = re.sub(r"^[a-z]+", "", head)
        revision = [int(inline)] if inline.isdigit() else []
        return tail, (4, tuple(revision + digits))
    rank = _PRERELEASE_RANK.get(label, 2)
    return tail, (rank, tuple(digits))


def is_outdated(client: str | None, minimum: str | None) -> bool:
    """客户端版本是否低于最低版本；任一方无法解析时视为不过期。"""
    left, right = version_key(client), version_key(minimum)
    if left is None or right is None:
        return False
    return left < right