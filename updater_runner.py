"""
更新安装器（独立程序，打包为 updater.exe）

用法:
    updater.exe --staging <zip路径> --install <安装目录> [--app <exe名>]

流程:
    1. 等待主程序 exe 完全退出（文件锁探测，最多 60 秒）
    2. 备份当前安装目录（排除临时锁文件）
    3. 解压新版 zip 覆盖安装目录
    4. 启动新版本主程序

说明:
    - 安装目录不包含用户数据（APPDATA/CottonAgent），更新不影响
      chroma_db / 会话记录 / .env
    - 无任何 GUI 依赖，可被 PyInstaller 最小化打包
"""

import argparse
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

DEFAULT_APP = "CottonAgent.exe"
WAIT_TIMEOUT = 60  # 秒


def _wait_for_exit(install_dir: Path, app_name: str, timeout: int = WAIT_TIMEOUT) -> bool:
    """等待主 exe 释放文件锁（可成功重命名即视为已退出）。"""
    main_exe = install_dir / app_name
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            probe = main_exe.with_name(main_exe.name + ".lockprobe")
            os.rename(str(main_exe), str(probe))
            os.rename(str(probe), str(main_exe))
            return True
        except OSError:
            time.sleep(1)
    return False


def _backup(install_dir: Path) -> Path:
    """备份当前安装目录到同级 CottonAgent_backup/。"""
    backup = install_dir.parent / "CottonAgent_backup"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(
        install_dir, backup,
        ignore=shutil.ignore_patterns("*.lockprobe"),
    )
    return backup


def _extract_over(zip_path: Path, install_dir: Path) -> None:
    """解压 zip 覆盖安装目录（保留未包含在包内的文件）。"""
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(install_dir)


def main() -> int:
    ap = argparse.ArgumentParser(description="棉花助手更新安装器")
    ap.add_argument("--staging", required=True, help="新版 zip 路径")
    ap.add_argument("--install", required=True, help="安装目录（exe 所在目录）")
    ap.add_argument("--app", default=DEFAULT_APP, help="主程序 exe 文件名")
    args = ap.parse_args()

    install_dir = Path(args.install)
    zip_path = Path(args.staging)
    app_name = args.app

    if not zip_path.exists():
        print(f"[updater] 更新包不存在: {zip_path}")
        return 1

    print("[updater] 等待主程序退出...")
    if not _wait_for_exit(install_dir, app_name):
        print("[updater] 等待超时，放弃更新。")
        return 1

    try:
        print("[updater] 备份当前版本...")
        _backup(install_dir)
        print("[updater] 解压新版...")
        _extract_over(zip_path, install_dir)
    except Exception as e:
        print(f"[updater] 安装失败: {e}")
        return 1

    new_exe = install_dir / app_name
    if new_exe.exists():
        print("[updater] 启动新版本...")
        os.startfile(str(new_exe))  # type: ignore[attr-defined]

    print("[updater] 更新完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
