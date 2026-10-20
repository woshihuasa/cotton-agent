# -*- coding: utf-8 -*-
"""打包产物路径的**单一真相**。

为什么单独抽一个模块（v0.3.3）：
    原先 `build_exe.py` / `build_release.py` / `tests/check_secrets.py` / `.gitignore`
    各自写死 `dist/`、`build/`，四处必须手动保持一致 —— 这次把产物从根目录收进
    `release/` 就同时要改 5 个常量，正是这种散落的代价。

目录结构：
    release/
    ├── build/                  PyInstaller 中间产物（--workpath，可随时删）
    ├── dist/CottonAgent/       开发产物，可直接双击试跑（--distpath）
    ├── CottonAgent-<tag>/      发布目录（剔除运行期残留后的干净副本）
    └── CottonAgent-<tag>.zip   发布包

为什么 `release/` 而不是别的名字：PyInstaller 的 spec 默认仍写死 `dist/`、`build/`，
    所以**直接用 `pyinstaller cotton_agent.spec`（不传 --distpath）仍会落到根目录**。
    `.gitignore` 因此同时保留 `dist/`、`build/` 与新名字，两边都挡。

用法：
    from paths import RELEASE_DIR, DIST_DIR, BUILD_DIR
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 打包产物统一收纳目录
RELEASE_DIR = ROOT / "release"

# PyInstaller 的 --distpath（开发产物，可试跑）
DIST_DIR = RELEASE_DIR / "dist"

# PyInstaller 的 --workpath（中间产物）
BUILD_DIR = RELEASE_DIR / "build"

# 开发产物的具体文件夹（build_exe.py 的成品）
APP_DIR = DIST_DIR / "CottonAgent"

# 更新器单文件产物
UPDATER_EXE = DIST_DIR / "updater.exe"


def ensure() -> None:
    """确保两个输出目录存在（构建前调用）。"""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)


def spec_args() -> list[str]:
    """返回所有打包命令都应附带的 `--workpath/--distpath` 参数。

    抽成函数是为了杜绝"漏传参数导致产物落回根目录 dist/"这种事故。
    """
    return ["--workpath", str(BUILD_DIR), "--distpath", str(DIST_DIR)]