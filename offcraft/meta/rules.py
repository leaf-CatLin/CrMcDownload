"""版本 JSON 中的 ``rules`` 匹配（操作系统、架构、features）。"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "OsInfo",
    "detect_os",
    "is_allowed",
    "native_classifier",
    "rule_matches",
]


@dataclass(frozen=True, slots=True)
class OsInfo:
    name: str = "windows"
    version: str = "10.0"
    arch: str = "x86_64"

    @property
    def is_64bit(self) -> bool:
        return self.arch in ("x86_64", "amd64", "aarch64")


def detect_os() -> OsInfo:
    system = platform.system().lower()
    name = {"windows": "windows", "darwin": "osx", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    # ``platform.machine()`` 在 Windows 上常返回 ``AMD64``，在 Linux/aarch64 返回
    # ``aarch64``。统一映射成 Mojang 规则里的 ``arch`` 值：x86 / x86_64 / arm64。
    if machine in ("amd64", "x86_64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = "x86"
    version = platform.version() or "10.0"
    return OsInfo(name=name, version=version, arch=arch)


def _os_matches(rule_os: Mapping[str, Any], os_info: OsInfo) -> bool:
    name = rule_os.get("name")
    if name is not None and name != os_info.name:
        return False
    arch = rule_os.get("arch")
    if arch is not None:
        if arch in ("x86_64", "amd64"):
            if os_info.arch not in ("x86_64", "amd64"):
                return False
        elif arch == "x86":
            if os_info.arch != "x86":
                return False
        elif arch == "arm64":
            if os_info.arch not in ("arm64", "aarch64"):
                return False
        else:
            # 其他显式架构（如 ``arm``/``mips``）暂未建模，宽松处理为不误伤
            if os_info.arch != arch:
                return False
    version = rule_os.get("version")
    if version is not None:
        try:
            if not re.search(version, os_info.version):
                return False
        except re.error:
            return False
    return True


def _features_match(
    rule_features: Mapping[str, Any], features: Mapping[str, bool]
) -> bool:
    for key, expected in rule_features.items():
        if bool(features.get(key, False)) != bool(expected):
            return False
    return True


def rule_matches(
    rule: Mapping[str, Any],
    os_info: OsInfo,
    features: Mapping[str, bool] | None = None,
) -> bool:
    """单条 rule 是否命中。"""
    features = features or {}
    rule_os = rule.get("os")
    if rule_os and not _os_matches(rule_os, os_info):
        return False
    rule_features = rule.get("features")
    if rule_features and not _features_match(rule_features, features):
        return False
    return True


def is_allowed(
    rules: list[Mapping[str, Any]] | None,
    os_info: OsInfo,
    features: Mapping[str, bool] | None = None,
) -> bool:
    """按 Mojang 语义判定：无 rules 视为允许，命中规则按顺序覆盖。"""
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if rule_matches(rule, os_info, features):
            allowed = rule.get("action") == "allow"
    return allowed


def native_classifier(library: Mapping[str, Any], os_info: OsInfo) -> str | None:
    """取版本 JSON ``natives`` 表里匹配当前平台 + 架构的分类器值。

    Mojang ``natives`` 表的键是 ``os.name``（如 ``windows``），arm64 平台还会出现
    ``windows-arm64`` / ``osx-arm64`` / ``linux-arm64`` 键。优先选带 ``-arm64`` 的
    具体键，否则回退到平台默认键；顺序固定保证行为可测。
    """
    natives = library.get("natives") or {}
    if not isinstance(natives, Mapping):
        return None
    os_key = os_info.name
    is_arm = os_info.arch in ("arm64", "aarch64")
    candidates = [f"{os_key}-arm64", os_key] if is_arm else [os_key]
    for key in candidates:
        value = natives.get(key)
        if value:
            return value
    return None
