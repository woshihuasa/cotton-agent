"""
GitHub 更新检查与下载

职责：
  - 检查 GitHub Releases 是否有新版本（后台线程调用，异常静默）
  - 下载新版 zip 到 APPDATA/update_staging/（支持进度回调）
  - 版本号比较（剥离 v 前缀）

使用方：ui/main_window.py 启动后延迟检查 → 用户确认 → 下载 →
        提示重启 → aboutToQuit 拉起 updater.exe 完成安装。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import requests

from config import APP_VERSION, AppConfig, app_data_dir

STAGING_DIR: Path = app_data_dir() / "update_staging"


def _parse_version(v: str) -> tuple:
    """'v1.2.3-beta' → (1, 2, 3)；无法解析时返回 (0, 0, 0)。"""
    parts = re.findall(r"\d+", re.sub(r"^v", "", v.strip()))
    nums = tuple(int(x) for x in parts[:3])
    return nums if nums else (0, 0, 0)


def check_for_update(timeout: int = 8) -> dict | None:
    """检查 GitHub 最新 Release。

    Returns:
        dict: {"version", "notes", "asset_url"}；无更新 / 未配置 /
              网络异常 / 非 200 一律返回 None（静默跳过）。
    """
    owner = AppConfig.GITHUB_OWNER.strip()
    repo = AppConfig.GITHUB_REPO.strip()
    if not owner or not repo:
        return None

    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        remote = _parse_version(data.get("tag_name", ""))
        local = _parse_version(APP_VERSION)
        if remote <= local:
            return None
        asset = next(
            (a for a in (data.get("assets") or [])
             if a.get("name", "").endswith(".zip")),
            None,
        )
        if not asset:
            return None
        return {
            "version": data.get("tag_name", "未知版本"),
            "notes": (data.get("body") or "")[:500],
            "asset_url": asset.get("browser_download_url", ""),
        }
    except Exception:
        return None


def download_asset(url: str, dest_dir: Path = STAGING_DIR,
                   progress_cb: Callable[[float], None] | None = None) -> Path:
    """下载 Release 资产（zip）到 staging 目录。

    Args:
        url: 资产下载直链。
        dest_dir: 保存目录。
        progress_cb: 进度回调（0.0-1.0）。

    Returns:
        Path: 下载完成的本地 zip 路径。

    Raises:
        requests.HTTPError: 下载失败时抛出。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / url.split("/")[-1].split("?")[0]

    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        done = 0
        with open(local, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(min(done / total, 1.0))
    return local
