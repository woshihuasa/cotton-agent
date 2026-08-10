"""
应用配置模块
集中管理所有环境变量和路径配置，通过 python-dotenv 加载 .env 文件。

路径三区分离（打包为 exe 后）：
  - 只读资产（data/、ui/icons/）→ sys._MEIPASS（打包内，用户不可改）
  - 运行数据（chroma_db、会话、图表）→ %APPDATA%/CottonAgent（更新不丢失）
  - 配置 .env → %APPDATA%/CottonAgent/.env（API Key 持久化）
开发模式（python main.py）：全部指向项目根，行为与之前一致。
"""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

# PyInstaller 打包后 sys.frozen 存在
IS_FROZEN: bool = bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    """打包后返回 _MEIPASS（解压资源目录），开发模式返回项目根。"""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def resource_path(rel: str) -> Path:
    """只读资源路径：data/、ui/icons/ 等打包内资产。"""
    return _base_dir() / rel


def app_data_dir() -> Path:
    """运行数据目录：打包后 %APPDATA%/CottonAgent，开发模式项目根。"""
    if IS_FROZEN:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "CottonAgent"
    else:
        base = Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


def app_version() -> str:
    """应用版本号。

    打包模式：读取打包内 build_version.txt（build_exe.py 写入 git describe 结果）；
    开发模式：实时读取 git describe；均失败时返回 dev 版本。
    """
    v = os.getenv("APP_VERSION", "").strip()
    if v:
        return v
    if IS_FROZEN:
        vf = resource_path("build_version.txt")
        if vf.exists():
            try:
                return vf.read_text("utf-8").strip() or "0.0.0-dev"
            except OSError:
                pass
        return "0.0.0-dev"
    try:
        v = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if v:
            return v
    except Exception:
        pass
    return "0.0.0-dev"


APP_VERSION: str = app_version()

# 加载 .env（打包后位于 APPDATA/CottonAgent/.env，开发模式位于项目根）
_dotenv_path = app_data_dir() / ".env"
load_dotenv(_dotenv_path)


class AppConfig:
    """应用全局配置类，以类属性形式集中管理所有配置项。"""

    # ── DeepSeek API 配置 ──
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
    )
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

    # ── 高德地图 API 配置 ──
    AMAP_API_KEY: str = os.getenv("AMAP_API_KEY", "")

    # ── Tavily 搜索 API 配置 ──
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

    # ── Embedding API 配置 (硅基流动) ──
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_BASE_URL: str = os.getenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")

    # ── GitHub 更新检查 ──
    GITHUB_OWNER: str = os.getenv("GITHUB_OWNER", "woshihuasa")
    GITHUB_REPO: str = os.getenv("GITHUB_REPO", "cotton-agent")

    # ── 数据与持久化路径（三区分离）──
    DATA_DIR: str = str(resource_path("data"))              # 只读知识库文档
    CHROMA_DB_PATH: str = str(app_data_dir() / "chroma_db")  # 向量库（可重建）
    USER_MEMORY_DB_PATH: str = str(app_data_dir() / "chroma_user_memory")  # L4 记忆
    SESSION_FILE_PATH: str = str(app_data_dir() / "session_state.json")    # L2+L3
    CHART_DIR: str = str(app_data_dir() / "charts")         # 图表输出目录


# 可通过设置对话框修改的配置键（reload_config 只刷新这些，不动路径常量）
_RUNTIME_KEYS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "AMAP_API_KEY", "TAVILY_API_KEY",
    "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL",
    "GITHUB_OWNER", "GITHUB_REPO",
)


def reload_config() -> None:
    """重新读取 .env 并刷新 AppConfig 的运行时配置属性。

    用户在设置对话框保存后调用：写入 .env → 本函数刷新内存中的
    类属性，使新配置（如 Embedding API Key）立即生效，无需重启。
    路径常量（DATA_DIR 等）不属于运行时配置，不在此刷新。
    """
    from dotenv import dotenv_values

    vals = dotenv_values(_dotenv_path)
    for k in _RUNTIME_KEYS:
        v = vals.get(k)
        if v is not None:
            setattr(AppConfig, k, v.strip())
        else:
            # .env 中删除了该键 → 回退默认值
            defaults = {
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "DEEPSEEK_MODEL": "deepseek-v4-flash",
                "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
                "EMBEDDING_MODEL": "BAAI/bge-large-zh-v1.5",
                "GITHUB_OWNER": "woshihuasa",
                "GITHUB_REPO": "cotton-agent",
            }
            setattr(AppConfig, k, defaults.get(k, ""))
