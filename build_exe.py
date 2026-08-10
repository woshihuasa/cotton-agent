"""
打包构建脚本

流程：
  1. 从 git describe 读取版本号 → 写入 version.txt（供 config 读取 APP_VERSION）
  2. 调用 PyInstaller 构建 CottonAgent（--onedir）
  3. 单独打包 updater_runner.py 为 updater.exe，复制进安装目录

用法：
  python build_exe.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist" / "CottonAgent"


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
    """构建主程序。"""
    print(f"== 构建 CottonAgent v{version} ==")
    # 版本号写入打包内文件（运行时从 _MEIPASS 读取）
    (ROOT / "build_version.txt").write_text(version, "utf-8")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm",
        str(ROOT / "cotton_agent.spec"),
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)
    print("主程序构建完成。")
    # 清理临时版本文件
    (ROOT / "build_version.txt").unlink(missing_ok=True)


def build_updater() -> None:
    """构建 updater.exe（无 GUI 最小程序）并复制到安装目录。"""
    print("== 构建 updater.exe ==")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconfirm", "--name", "updater",
        "--console",
        str(ROOT / "updater_runner.py"),
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)
    updater_exe = ROOT / "dist" / "updater.exe"
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
