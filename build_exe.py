"""
打包构建脚本

流程：
  1. 从 git describe 读取版本号 → 写入 build_version.txt（供 runtime 读 APP_VERSION）
  2. 调用 PyInstaller 构建 CottonAgent（--onedir）
  3. 单独打包 updater_runner.py 为 updater.exe，复制进安装目录

产物位置统一由 `paths.py` 决定（当前 = `release/dist/CottonAgent/`）。

用法：
  python build_exe.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

import paths

ROOT = paths.ROOT
DIST_DIR = paths.APP_DIR           # release/dist/CottonAgent


def git_version() -> str:
    """git describe → 版本号；非 git 仓库返回 dev。"""
    try:
        v = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5, cwd=ROOT,
        ).stdout.strip()
        return v or "0.0.0-dev"
    except Exception:
        return "0.0.0-dev"


def build_app(version: str) -> None:
    """构建主程序。

    ⚠️ `build_version.txt` 构建后**必须保留，不要删**：
    它是 `cotton_agent.spec` 的 datas 输入（`(str(ROOT / "build_version.txt"), ".")`），
    会被打包进 _MEIPASS 供运行时读取 APP_VERSION。早期实现在构建结束时把它删掉，
    于是之后任何一次"直接用 spec 打包"（`pyinstaller cotton_agent.spec`）都会以
        SystemExit: ERROR: Unable to find '...\\build_version.txt' when adding
        binary and data files.
    失败 —— 该报错**没有 traceback、退出码 1**，极难定位（v0.3.3 排查打包问题
    时正是被同一类错误卡了三个轮次）。文件已在 .gitignore 中，保留不污染工作区。
    """
    print(f"== 构建 CottonAgent v{version} ==")
    # 版本号写入打包内文件（运行时从 _MEIPASS 读取）
    (ROOT / "build_version.txt").write_text(version, "utf-8")
    paths.ensure()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm",
        *paths.spec_args(),                 # --workpath/--distpath 统一从 paths 取
        str(ROOT / "cotton_agent.spec"),
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)
    print("主程序构建完成。")


def build_updater() -> None:
    """构建 updater.exe（无 GUI 最小程序）并复制到安装目录。"""
    print("== 构建 updater.exe ==")
    paths.ensure()
    # ★★ 2026-10-20 修：**必须传 `.spec`，不能传 `.py`**。
    #
    #   传 `.py` 时 PyInstaller 会**自动生成** `updater.spec` 并写回仓库根，
    #   而生成版第 5 行写死绝对路径 `E:\cotton_agent\updater_runner.py`。
    #   结果就是：仓库里那份已修好的 spec（用 `__file__` 推导根目录，
    #   注释原话「别人 clone 下来打包必然失败」）**每次构建都被静默回滚**。
    #
    #   传 `.spec` 时 PyInstaller 以它为准，**不会**再生成 —— 修复得以保留。
    #   附带好处：spec 里已声明 console=True、EXE 单文件，不用再传 `--onefile`。
    spec_file = ROOT / "updater.spec"
    if not spec_file.is_file():
        raise SystemExit(f"缺少 updater.spec：{spec_file}")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        *paths.spec_args(),
        str(spec_file),
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)
    updater_exe = paths.UPDATER_EXE
    if updater_exe.exists() and DIST_DIR.exists():
        shutil.copy2(updater_exe, DIST_DIR / "updater.exe")
        print(f"updater.exe 已复制到 {DIST_DIR}")
    else:
        print("警告: updater.exe 未生成或安装目录不存在")


def main() -> None:
    version = git_version()
    print(f"版本号: {version}")
    build_app(version)
    build_updater()
    print(f"\n打包完成: {DIST_DIR}")


if __name__ == "__main__":
    main()
