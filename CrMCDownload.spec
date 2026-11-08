# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：文件夹分发（onedir），避免单文件运行时解压临时目录失败。"""

import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# 虚拟局域网数据面（packaging/vpn/out，由 packaging/vpn/vpn-build.ps1 产出）：
# vpnd.exe / vpn-setup.exe / wintun.dll 必须和主程序同目录，GUI 才能在首次启用时提权安装。
_vpn_out = os.path.join(SPECPATH, "packaging", "vpn", "out")
_vpn_binaries = []
for _name in ("vpnd.exe", "vpn-setup.exe", "wintun.dll"):
    _path = os.path.join(_vpn_out, _name)
    if os.path.isfile(_path):
        _vpn_binaries.append((_path, "."))
_vpn_datas = []
_license = os.path.join(_vpn_out, "LICENSE.txt")
if os.path.isfile(_license):
    _vpn_datas.append((_license, "."))

# 皮肤校验 / 头像裁切用到 Pillow，onedir 产物必须显式收齐 PIL 子模块与 _imaging 扩展；
# 官方内置插件经 importlib 动态导入，静态分析抓不到，须一并收集 offcraft.plugins.builtin；
# 离线联机（P2P）用 pynacl（libsodium 绑定），收齐 nacl 子模块与其原生库（_sodium.pyd/libsodium.dll）；
# nacl._sodium 是 cffi 扩展，导入裸 _cffi_backend（不导入 cffi 包，PyInstaller 的 cffi hook 不触发），须显式收它。
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=collect_dynamic_libs("nacl") + _vpn_binaries,
    datas=[("assets/icon.ico", "assets"), ("assets/icon.png", "assets"), *_vpn_datas],
    hiddenimports=[
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.QtSvg",
        *collect_submodules("PIL"),
        *collect_submodules("offcraft.plugins.builtin"),
        *collect_submodules("nacl"),
        "_cffi_backend",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
# 离线联机（P2P）作为按需下载的官方特权插件：基础包排除纯 Python 引擎 offcraft.p2p
# （运行时由插件页下载/启用的 bundle 以顶层包 offcraft_p2p 提供）；nacl 暂仍随基础包保留
# （Phase 0 冻结 .pyd 加载的权威判定留待打包验证，未确证前走此后备，故不收 offcraft.p2p）。
excludes=["PyQt5", "paramiko", "pytest", "numpy", "pandas", "tkinter", "offcraft.p2p"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CrMCDownload",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_info.txt",
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="CrMCDownload",
)
