"""插件注册表：仅从内置白名单发现官方插件，启停状态与命名空间配置落盘。

安全边界：

- **import 白名单**：只加载 ``BUILTIN_MODULES`` 列出的模块，绝不扫描/执行第三方目录；
- ``api`` 主版本不匹配 → 标记 ``incompatible`` 且不调用 ``setup``；
- 单个插件 ``setup`` 抛错只记 ``error`` 状态，不影响其他插件与主程序。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offcraft.config import config_dir
from offcraft.plugins.base import PLUGIN_API, Plugin, PluginError, PluginManifest
from offcraft.plugins.bundle import BundleError, MANIFEST_FILE, read_manifest, verify_bundle
from offcraft.plugins.context import PluginContext, PluginHost

__all__ = ["PluginRecord", "PluginRegistry"]

#: 官方内置插件模块白名单（新增官方插件在此登记；第三方目录扫描刻意不做）
#: 目前没有随包内置的插件，离线联机以官方 bundle 形式按需下载安装。
BUILTIN_MODULES: tuple[str, ...] = ()

_STATE_FILE = "plugins.json"


@dataclass(slots=True)
class PluginRecord:
    manifest: PluginManifest
    enabled: bool = False
    #: loaded / disabled / incompatible / error
    status: str = "disabled"
    error: str = ""
    instance: Plugin | None = None
    context: PluginContext | None = None
    nav_items: list[tuple[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.manifest.id,
            "name": self.manifest.name,
            "description": self.manifest.description,
            "version": self.manifest.version,
            "api": self.manifest.api,
            "capabilities": list(self.manifest.capabilities),
            "official": self.manifest.official,
            "detail": self.manifest.detail,
            "enabled": self.enabled,
            "status": self.status,
            "error": self.error,
        }


class PluginRegistry:
    def __init__(self, data_dir: str | Path | None = None, host: PluginHost | None = None) -> None:
        self.data_dir = data_dir
        self.host = host
        self.path = config_dir(data_dir) / _STATE_FILE
        self.records: list[PluginRecord] = []
        self._enabled: dict[str, bool] = {}
        self._config: dict[str, Any] = {}
        self._loaded = False

    # -- 持久化 --------------------------------------------------------
    def _read_state(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            enabled = data.get("enabled") or {}
            self._enabled = {str(k): bool(v) for k, v in enabled.items()}
            config = data.get("config") or {}
            self._config = dict(config) if isinstance(config, dict) else {}

    def save(self) -> None:
        directory = config_dir(self.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": {rec.manifest.id: rec.enabled for rec in self.records} or self._enabled,
            "config": self._config,
        }
        tmp = self.path.with_name(_STATE_FILE + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    # -- 发现与加载 ----------------------------------------------------
    def reload(self) -> list[PluginRecord]:
        """重新扫描内置 + 已下载插件（用于插件下载/卸载后刷新）。

        先卸载现有实例并清除已下载入口模块缓存，再重新发现——保证新落地的
        bundle 能被扫到、旧的能被替换。
        """
        self.unload_all()
        for name in [n for n in sys.modules if n.startswith("_offcraft_plugin_")]:
            sys.modules.pop(name, None)
        self.records = []
        self._loaded = False
        return self.load()

    def load(self) -> list[PluginRecord]:
        """一次性发现并加载内置 + 已下载官方插件（幂等）。"""
        if self._loaded:
            return self.records
        self._loaded = True
        self._read_state()
        for module_name in BUILTIN_MODULES:
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - 内置模块坏了也不崩主程序
                self._add_error(module_name, f"模块导入失败：{exc}")
                continue
            manifest = getattr(module, "MANIFEST", None)
            factory = getattr(module, "create", None)
            if not isinstance(manifest, PluginManifest) or not callable(factory):
                self._add_error(module_name, "缺少 MANIFEST 或 create()")
                continue
            self._register(module, manifest, factory)
        self._load_downloaded()
        return self.records

    # -- 已下载官方插件（按需加载）--------------------------------
    def _load_downloaded(self) -> None:
        """扫描 ``config_dir()/plugins/<id>/`` 下带 manifest.json 的第一方 bundle。

        仅扫描本程序自己写入的插件目录，且加载前逐文件校验 sha256（见
        :mod:`offcraft.plugins.bundle`）——校验不过绝不 import，第三方动态加载始终禁止。
        """
        base = config_dir(self.data_dir) / "plugins"
        if not base.is_dir():
            return
        for child in sorted(base.iterdir()):
            if not child.is_dir() or not (child / MANIFEST_FILE).is_file():
                continue
            self._register_bundle(child)

    def _register_bundle(self, directory: Path) -> None:
        plugin_id = directory.name
        if not verify_bundle(self.data_dir, plugin_id):
            self._add_error(plugin_id, "插件完整性校验失败（文件与安装记录不符，已拒绝加载）")
            return
        try:
            meta = read_manifest(self.data_dir, plugin_id)
        except (BundleError, OSError, ValueError) as exc:
            self._add_error(plugin_id, f"清单解析失败：{exc}")
            return
        entry = str(meta.get("entry") or "plugin_main.py")
        module = self._import_entry(directory, plugin_id, entry)
        if module is None:
            return
        manifest = getattr(module, "MANIFEST", None)
        factory = getattr(module, "create", None)
        if not isinstance(manifest, PluginManifest) or not callable(factory):
            self._add_error(plugin_id, "入口缺少 MANIFEST 或 create()")
            return
        # 下载后的官方插件默认不自动启用（由插件页卡片状态机控制）
        self._register(module, manifest, factory, default_enabled=False)

    def _import_entry(self, directory: Path, plugin_id: str, entry_file: str):
        path = directory / entry_file
        if not path.is_file():
            self._add_error(plugin_id, f"入口文件缺失：{entry_file}")
            return None
        module_name = f"_offcraft_plugin_{plugin_id}"
        sys.path.insert(0, str(directory))  # 让 bundle 内的引擎/依赖可被 import
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                self._add_error(plugin_id, "无法构建模块规格")
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - bundle 坏了不阻断
            sys.modules.pop(module_name, None)
            self._add_error(plugin_id, f"入口加载失败：{exc}")
            return None
        return module

    def _add_error(self, module_name: str, message: str) -> None:
        stub = PluginManifest(
            id=module_name.rsplit(".", 1)[-1],
            name=module_name,
            description="",
            version="0",
            api=PLUGIN_API,
        )
        self.records.append(PluginRecord(manifest=stub, status="error", error=message))

    def _register(self, module, manifest: PluginManifest, factory, *, default_enabled=None) -> None:
        fallback = manifest.official if default_enabled is None else default_enabled
        enabled = bool(self._enabled.get(manifest.id, fallback))
        try:
            manifest.validate()
        except PluginError as exc:
            self.records.append(
                PluginRecord(manifest=manifest, enabled=False, status="incompatible", error=str(exc))
            )
            return
        try:
            instance = factory()
        except Exception as exc:  # noqa: BLE001
            self.records.append(
                PluginRecord(manifest=manifest, enabled=False, status="error", error=f"实例化失败：{exc}")
            )
            return
        record = PluginRecord(manifest=manifest, instance=instance, enabled=enabled)
        self.records.append(record)
        if enabled and self.host is not None:
            self._setup(record)

    def _setup(self, record: PluginRecord) -> None:
        assert record.instance is not None and self.host is not None
        context = PluginContext(
            record.manifest.id, record.manifest.capabilities, self.host, self._config
        )
        record.context = context
        try:
            record.instance.setup(context)
            record.status = "loaded"
        except Exception as exc:  # noqa: BLE001 - 插件坏了不拖累其他
            record.status = "error"
            record.error = str(exc)

    # -- 启停 ----------------------------------------------------------
    def get(self, plugin_id: str) -> PluginRecord | None:
        for record in self.records:
            if record.manifest.id == plugin_id:
                return record
        return None

    def set_enabled(self, plugin_id: str, enabled: bool) -> bool:
        record = self.get(plugin_id)
        if record is None or record.status == "incompatible":
            return False
        if record.enabled == enabled:
            return True
        record.enabled = enabled
        if enabled and self.host is not None:
            self._setup(record)
        else:
            self._teardown(record)
            record.status = "disabled"
        self.save()
        return True

    def _teardown(self, record: PluginRecord) -> None:
        if record.instance is not None and record.context is not None:
            try:
                record.instance.teardown()
            except Exception:  # noqa: BLE001 - 卸载失败静默
                pass
        record.context = None

    def unload_all(self) -> None:
        for record in self.records:
            self._teardown(record)

    def list(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self.records]
