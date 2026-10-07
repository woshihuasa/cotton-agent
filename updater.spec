# -*- mode: python ; coding: utf-8 -*-
"""更新器（updater）的 PyInstaller 配置。

⚠️ 原先第 5 行是**写死的绝对路径** `E:\\cotton_agent\\updater_runner.py` ——
   别人 clone 下来打包必然失败。改为与 `cotton_agent.spec` 一致的做法：
   以 spec 文件自身位置推导仓库根目录。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent if "__file__" in globals() else Path(SPECPATH)


a = Analysis(
    [str(ROOT / "updater_runner.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='updater',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
