# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=collect_dynamic_libs('nacl'),
    datas=[],
    hiddenimports=(
        collect_submodules('PIL')
        + collect_submodules('offcraft.plugins.builtin')
        + collect_submodules('nacl')
        + ['_cffi_backend', 'webview', 'webview.platforms.edgechromium', 'clr', 'clr_loader']
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['paramiko', 'offcraft.p2p', 'PyQt5'],  # P2P 引擎随插件按需下载（以 offcraft_p2p 提供），基础包不内置
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OffCraftDebug',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['_sodium.pyd'],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
)
