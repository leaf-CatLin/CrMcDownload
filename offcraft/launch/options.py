"""``options.txt`` 的轻量读写：把资源包加入 ``resourcePacks`` 列表。

Minecraft 只有在 ``resourcePacks`` 里出现 ``file/<文件名>``（zip）或
``folder/<目录名>``（文件夹）时才会加载对应资源包。启动器下载资源包后
默认只是放进目录，不会自动启用，表现就是「资源包没有加载」。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = [
    "add_resource_packs",
    "enable_all_resource_packs",
    "set_resource_pack_enabled",
]

OPTIONS_NAME = "options.txt"
_KEY = "resourcePacks:"


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_value(line: str) -> list[str]:
    value = line[len(_KEY) :].strip()
    try:
        data = json.loads(value)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _entry_for(path: Path) -> str:
    kind = "folder" if path.is_dir() else "file"
    return f"{kind}/{path.name}"


def _pack_names(game_dir: Path) -> list[Path]:
    folder = Path(game_dir) / "resourcepacks"
    if not folder.is_dir():
        return []
    packs = [
        item
        for item in folder.iterdir()
        if item.is_dir() or item.suffix.lower() in (".zip", ".jar")
    ]
    packs.sort(key=lambda item: item.name.lower())
    return packs


def add_resource_packs(game_dir: Path | str, paths: list[Path]) -> int:
    """把资源包加入 ``resourcePacks``，返回新增条数（已启用则跳过）。"""
    game_dir = Path(game_dir)
    options = game_dir / OPTIONS_NAME
    lines = _read_lines(options)
    index = next((i for i, line in enumerate(lines) if line.startswith(_KEY)), -1)
    if index >= 0:
        current = _parse_value(lines[index])
    else:
        current = ["vanilla"]
        lines.append(_KEY + json.dumps(current, ensure_ascii=False, separators=(",", ":")))
        index = len(lines) - 1

    added = 0
    for path in paths:
        entry = _entry_for(Path(path))
        if entry in current:
            continue
        current.append(entry)
        added += 1
    if added:
        lines[index] = _KEY + json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        _write_lines(options, lines)
    return added


def enable_all_resource_packs(game_dir: Path | str) -> int:
    """``options.txt`` 还没有 ``resourcePacks`` 时，启用目录里的全部资源包。

    已有该键说明玩家在游戏里设置过，不再改动，避免覆盖玩家的选择。
    """
    game_dir = Path(game_dir)
    options = game_dir / OPTIONS_NAME
    if any(line.startswith(_KEY) for line in _read_lines(options)):
        return 0
    return add_resource_packs(game_dir, _pack_names(game_dir))


def set_resource_pack_enabled(
    game_dir: Path | str, name: str, enabled: bool
) -> bool:
    """把单个资源包加入 / 移出 ``resourcePacks``，返回是否真的改动了文件。"""
    game_dir = Path(game_dir)
    options = game_dir / OPTIONS_NAME
    pack = game_dir / "resourcepacks" / name
    entry = f"{'folder' if pack.is_dir() else 'file'}/{name}"
    lines = _read_lines(options)
    index = next((i for i, line in enumerate(lines) if line.startswith(_KEY)), -1)
    if index >= 0:
        current = _parse_value(lines[index])
    else:
        current = ["vanilla"]
        lines.append(_KEY + json.dumps(current, ensure_ascii=False, separators=(",", ":")))
        index = len(lines) - 1

    if enabled:
        if entry in current:
            return False
        current.append(entry)
    else:
        if entry not in current:
            return False
        current = [item for item in current if item != entry]
    lines[index] = _KEY + json.dumps(current, ensure_ascii=False, separators=(",", ":"))
    _write_lines(options, lines)
    return True
