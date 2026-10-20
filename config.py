"""
应用配置模块
集中管理所有环境变量和路径配置，通过 python-dotenv 加载 .env 文件。

路径三区分离（打包为 exe 后）：
  - 只读资产（data/、ui/icons/）→ sys._MEIPASS（打包内，用户不可改）
  - 运行数据（chroma_db、会话、图表）→ %APPDATA%/CottonAgent（更新不丢失）
  - 配置 .env → 运行数据目录下的 .env（API Key 持久化）
开发模式（python main.py）：全部指向项目根，行为与之前一致。

⚠️ v0.3.3：运行数据目录在 %APPDATA% **不可写**时会降级到 exe 同级 runtime_data/
（实测安全软件的文件系统过滤驱动会拒绝未签名程序写用户目录，报 WinError 13，
而 NTFS 权限项一切正常）。降级后仍会在所有候选位置里寻找**已有的** .env，
**不再因此丢掉用户配置**。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import bootstrap
from bootstrap import ensure_runtime_dirs, ensure_writable_temp, find_env_file
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
    """运行数据目录，带多级降级，**永不抛异常**。

    优先级（打包模式）：
      1. %APPDATA%/CottonAgent       —— 首选，更新不丢失
      2. %LOCALAPPDATA%/CottonAgent  —— 漫游禁用/被锁时的本机回退
      3. exe 同级 runtime_data/      —— 用户目录整体不可写时的兜底（便携模式）
      4. %TEMP%/CottonAgent          —— 最后退路（重启可能丢）
    开发模式固定返回项目根。

    实现委托给 `bootstrap.app_data_dir()`，两处共用同一份逻辑。那里用"真写一个
    探针文件再删掉"来判定可写 —— 因为安全软件的拦截发生在**系统调用**层，只看
    NTFS 权限位会得出错误结论（实测：权限项是 Administrator 完全控制，写入仍被
    360 的文件系统过滤驱动以 WinError 13 拒绝；`os.access` 也报告可写）。

    ⚠️ 为什么必须永不抛（v0.3.3 修复）：
    本函数在 config.py 模块顶层就被调用（决定 _dotenv_path）。
    若此处抛 PermissionError，整个 config 模块导入失败，而 main.py 的
    excepthook 与日志初始化都在 `import config` **之后**——应用直接静默消失，
    不留日志、不弹错误框。这是 v0.3.2 在多台机器"打不开且零信息"的真实根因。
    """
    return bootstrap.app_data_dir()


# ★ tiktoken 词表缓存目录（2026-10-20 加）。
#
# 为什么放在 `config` 的模块顶层：`config` 会被**所有**入口 import
# （`main.py` / `server.py` / `mcp_server.py` / 各守卫脚本），
# 放在这里就一处覆盖全部；放在 main.py 则只覆盖桌面端。
#
# 为什么必须钉住：`tiktoken` 找不到缓存时会去
# `openaipublic.blob.core.windows.net` 下载词表。它的默认查找位置是
# `tempfile.gettempdir()/data-gym-cache`，而本机 `%TEMP%` 不可写 →
# `gettempdir()` 退化到**当前工作目录**且结果被缓存 → 目录随 cwd 变。
# 实测后果：打包版离线直接抛 `SSLError`；守卫 `check_handoff` 时绿时红。
try:
    bootstrap.tiktoken_cache_dir()
except Exception:                                         # noqa: BLE001
    # 永不因为缓存目录失败而阻止 import —— 与 app_data_dir 同一条纪律
    pass


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

# 临时目录必须在 chromadb / matplotlib 被导入之前就指到可写位置：
# 否则新建向量库必然报 `code: 14 unable to open database file`（见 bootstrap 注释）。
_data_dir = app_data_dir()
ensure_writable_temp(_data_dir)

# ── .env ──
# **权威位置**是运行数据目录下的 .env：设置对话框（core/config_manager.ENV_PATH）
# 与 reload_config() 都读写这里。
_dotenv_path = _data_dir / ".env"

# 数据目录里还没有时，去别处找现成的配置（%APPDATA%/CottonAgent/.env → exe 同级
# → 项目根）。app_data_dir() 在 %APPDATA% 不可写时会降级到 exe 同级，而用户的
# .env 通常还留在 %APPDATA% 里 —— **写不进去但读得到**，降级不该等于丢配置。
#
# 找到后必须**一次性导入**到权威位置，而不是"读到就算"：设置对话框读的是权威
# 位置，只读不导就会出现"应用跑得好好的、设置里 API Key 却全是空白"这种自相矛盾
# （v0.3.3 实测）。导入失败不致命 —— 本次仍从原位置读取，只是设置对话框看不到。
_found_env = find_env_file(_data_dir)
if _found_env and _found_env != _dotenv_path and not _dotenv_path.exists():
    try:
        shutil.copy2(_found_env, _dotenv_path)
    except OSError:
        pass
load_dotenv(_dotenv_path if _dotenv_path.exists() else (_found_env or _dotenv_path))


# ── 技能挂载点（设计：docs/ARCHITECTURE.md §3–§4）──
# 手写技能**随版本分发** ⇒ 真相源在源码区，运行时通过配置**挂载为只读**、不复制。
# 与"结晶档"（引擎自动生成，落在运行区）刻意分开：
#   · 挂载点 = 人写、git 管、Agent **只读**
#   · 运行区 = 引擎写、引擎读（见 core/procedural_memory.py）
#
# 为什么不把 `skills/` 复制到用户目录：那会造成**两份真相**，而
# tests/smoke_mcp.py 守的正是"契约与本地定义逐字段一致"——复制之后守卫就守不住了。
SHIPPED_SKILL_DIR: str = os.getenv("SHIPPED_SKILL_DIR", "skills")
# 外部挂载的技能目录（独立技能仓库的本地 clone），用 os.pathsep 分隔多个绝对路径。
# 用途：只分享技能、不带上主应用代码，两边版本节奏解耦。
EXTERNAL_SKILL_DIRS_ENV: str = "EXTERNAL_SKILL_DIRS"

#: ── 轨迹录制（E4）──
#: **空串 = 关闭**（默认）。设成路径（如 `tests/eval_outputs/trajectory.jsonl`）即开启：
#: 每次问答会把**完整轨迹**（工具调用 / 推理 / 用量 / 路由）追加一行 JSONL。
#: 用 	ools/export_trajectory.py 可转成 ShareGPT 格式（HF datasets 可直接加载）。
#: ★ 关闭时 sk() 里只多一次空串判断，**行为与改动前逐字一致**。
TRAJECTORY_PATH: str = os.getenv("TRAJECTORY_PATH", "")


def shipped_skill_dir() -> Path:
    """随版本分发的手写技能目录。

    走 `resource_path()`（打包后位于 `_MEIPASS` 内）—— 与 `data/`、`ui/icons/`
    同属**只读资源**，因此天然满足"挂载点只读"。
    """
    return resource_path(SHIPPED_SKILL_DIR)


def external_skill_dirs() -> list[Path]:
    """外部挂载的额外技能目录。

    **不校验存在性** —— 挂载点缺失不该阻断启动，由加载器现扫时自然跳过。
    """
    out: list[Path] = []
    for part in (os.getenv(EXTERNAL_SKILL_DIRS_ENV, "") or "").split(os.pathsep):
        part = part.strip()
        if part:
            out.append(Path(part))
    return out


def skill_mount_dirs() -> list[Path]:
    """全部**只读**技能挂载点（人写、git 管、Agent 只读）。

    顺序：随版本分发的手写技能 → 外部挂载点。
    由 `core/procedural_memory.ProceduralMemoryStore` 与门禁判据共用 ——
    **单一真相**，避免两处各写一份挂载清单（那正是本项目一直在防的"两份真相"）。
    """
    return [shipped_skill_dir(), *external_skill_dirs()]


# 建出运行区目录（幂等、失败不阻断 —— 与 app_data_dir 同一条纪律）。
# **只建目录**：不注册、不复制、不写索引文件（ARCHITECTURE §4 约定 1）。
ensure_runtime_dirs(_data_dir)


class AppConfig:
    """应用全局配置类，以类属性形式集中管理所有配置项。"""

    # ── DeepSeek API 配置 ──
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
    )
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

    # ── 默认模型服务商（`core/providers.py` 的登记名）──
    #
    # ★ **留空 = 完全沿用上面的 DEEPSEEK_\* 配置，行为与改动前逐字一致。**
    # 设为 `qwen` 等已登记的名字后，全项目的默认客户端改走该服务商
    # （端点与密钥来自登记表 + 凭据层，**不再读 DEEPSEEK_BASE_URL**）。
    #
    # 为什么用"服务商名"而不是直接改 DEEPSEEK_BASE_URL：
    # 把 Token Plan 的端点写进 `DEEPSEEK_BASE_URL` 会让**键名变成谎言**，
    # 而登记表里端点、密钥来源、兼容性备注是**一处维护**的。
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "").strip()

    #: 默认模型覆盖。留空 = 用该服务商的 `default_model`。
    LLM_MODEL: str = os.getenv("LLM_MODEL", "").strip()

    # ── 高德地图 API 配置 ──
    AMAP_API_KEY: str = os.getenv("AMAP_API_KEY", "")

    # ── Tavily 搜索 API 配置 ──
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

    # ── Embedding API 配置 (硅基流动) ──
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_BASE_URL: str = os.getenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")

    # ── Rerank 精排配置（复用硅基流动，同一 API Key）──
    # 开启后：向量召回 RERANK_RECALL_K 条 → CrossEncoder 精排 → 取 top-k
    RERANK_ENABLED: bool = os.getenv("RERANK_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    RERANK_MODEL: str = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    RERANK_RECALL_K: int = int(os.getenv("RERANK_RECALL_K", "20") or "20")

    # ── GitHub 更新检查 ──
    GITHUB_OWNER: str = os.getenv("GITHUB_OWNER", "woshihuasa")
    GITHUB_REPO: str = os.getenv("GITHUB_REPO", "cotton-agent")

    # ── Web 服务访问控制（公网 / 多设备场景）──
    # WEB_ACCESS_TOKEN 留空 = 不校验口令，适合本机与家庭局域网自用；
    # 一旦把服务暴露到公网，必须设置 —— 否则任何人都能消耗你的 Embedding 配额。
    WEB_ACCESS_TOKEN: str = os.getenv("WEB_ACCESS_TOKEN", "")
    WEB_RATE_PER_MIN: int = int(os.getenv("WEB_RATE_PER_MIN", "20") or "20")      # 单 IP 每分钟问答上限
    WEB_DAILY_LIMIT: int = int(os.getenv("WEB_DAILY_LIMIT", "2000") or "2000")   # 全站每日问答上限

    # ── 数据与持久化路径（三区分离）──
    DATA_DIR: str = str(resource_path("data"))              # 只读知识库文档
    CHROMA_DB_PATH: str = str(app_data_dir() / "chroma_db")  # 向量库（可重建）
    USER_MEMORY_DB_PATH: str = str(app_data_dir() / "chroma_user_memory")  # L4 记忆
    SESSION_FILE_PATH: str = str(app_data_dir() / "session_state.json")    # L2+L3
    CHART_DIR: str = str(app_data_dir() / "charts")         # 图表输出目录


# 可通过设置对话框修改的配置键（reload_config 只刷新这些，不动路径常量）
_RUNTIME_KEYS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "LLM_PROVIDER", "LLM_MODEL",          # 切换默认服务商 / 模型（热更新）
    "AMAP_API_KEY", "TAVILY_API_KEY",
    "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL",
    "GITHUB_OWNER", "GITHUB_REPO",
    "WEB_ACCESS_TOKEN",     # 限流参数改动频率低，重启生效即可，不在此热更新
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
                "DEEPSEEK_MODEL": "deepseek-flash",
                "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
                "EMBEDDING_MODEL": "BAAI/bge-large-zh-v1.5",
                "GITHUB_OWNER": "woshihuasa",
                "GITHUB_REPO": "cotton-agent",
            }
            setattr(AppConfig, k, defaults.get(k, ""))
