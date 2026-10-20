# -*- coding: utf-8 -*-
"""启动引导：在导入任何重库之前，把「可写数据目录」和「临时目录」定下来。

为什么必须最早执行 —— 每一条都是 v0.3.3 打包实测踩到的：

1. **chromadb 的 Rust 后端新建向量库时需要一个可用的临时目录**，拿不到就报
   `InternalError: error returned from database: (code: 14) unable to open
   database file`（SQLite 的 SQLITE_CANTOPEN）。旧库能打开、**新库**打不开，
   正是因为只有初始化新库才需要临时文件。
2. `tempfile.gettempdir()` 在 `%TEMP%` 不可写时会**退化到当前工作目录**；
   双击运行时那就是 exe 所在目录 —— 实测在 `dist/CottonAgent/` 下生成过
   `CottonAgent/CottonAgent/cotton_app.log` 这种谁都找不到的怪目录，
   因为日志走了 `gettempdir()/CottonAgent`。
3. matplotlib 需要可写的 `MPLCONFIGDIR`，否则每次启动都刷不出字体缓存
   （实测报 `PermissionError: ...fontlist-v3.11.0.json.matplotlib-lock`）。
4. PyInstaller 的 `pyi_rth_mplconfig` 找不到可用临时目录时会**直接终止启动**。

真实触发场景（实测于未签名 exe）：某些安全软件会拒绝**未签名程序**写入
`%APPDATA%` / `%LOCALAPPDATA%` / `%TEMP%`，而环境变量与 NTFS 权限项都完全
正常 —— 所以只能靠"写一个探针文件"来检测，不能靠 `os.access`。

设计约束：
  - 本模块**不导入** config / PyQt6 / matplotlib / dotenv 等任何重库，也不依赖
    第三方包：它必须能在 `main.py` 最顶部、`import config` **之前**跑起来。
  - 所有探测**永不抛异常**：失败就试下一个候选，全部失败也不阻断启动。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 打包后 sys.frozen 存在
IS_FROZEN: bool = bool(getattr(sys, "frozen", False))

_PROBE_NAME = ".write_probe"

# 临时目录子目录名（放在数据目录下，随数据目录一起可写）
_TEMP_SUBDIR = "tmp"
# matplotlib 缓存子目录名（独立于系统临时目录，字体缓存可长期复用）
_MPL_SUBDIR = ".mplconfig"


# ── 可写性探测 ─────────────────────────────────────────


def _try_writable(directory: Path) -> bool:
    """在目录里真写一个文件再删掉。

    为什么不用 `os.access(dir, os.W_OK)`：安全软件的拦截发生在**系统调用**
    层面，`os.access` 只看 NTFS 权限位，会给出"可写"的错误结论。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / _PROBE_NAME
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ── 运行数据目录 ───────────────────────────────────────


def data_dir_candidates() -> list[Path]:
    """运行数据目录候选，按优先级排列。

    打包模式：`%APPDATA%` → `%LOCALAPPDATA%` → exe 同级 `runtime_data`
    → `%TEMP%/CottonAgent`。
    开发模式：固定项目根（与升级前行为一致）。
    """
    # ★★ 环境变量覆盖（2026-10-20 加）——
    #   最高优先级，**开发模式也生效**。
    #
    #   为什么必须有：开发模式下这里固定返回项目根，于是
    #   **所有自动化测试都会写进真实数据目录**（会话、L4 记忆、密钥）。
    #   实测后果：跑了几轮守卫后 `session_state.json` 里多出 **85 个空会话**，
    #   把用户自己的对话淹掉了。
    #
    #   加了它之后，测试只需 `set COTTON_AGENT_DATA_DIR=<临时目录>`，
    #   就与真实数据完全隔离。顺带也支持"便携模式"（把数据钉在指定目录）。
    override = os.environ.get("COTTON_AGENT_DATA_DIR", "").strip()
    if override:
        return [Path(override)]

    if not IS_FROZEN:
        return [Path(__file__).resolve().parent]

    candidates: list[Path] = []
    for var in ("APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(var)
        if value:
            candidates.append(Path(value) / "CottonAgent")
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "runtime_data")
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if temp:
        candidates.append(Path(temp) / "CottonAgent")
    return candidates


def app_data_dir() -> Path:
    """第一个**确实可写**的数据目录；全部不可写时返回最后一个候选。

    永不抛异常 —— 本函数在 config.py 模块顶层就被调用，一旦抛
    PermissionError，整个 config 导入失败，而 main.py 的 excepthook 与日志
    初始化都在 `import config` 之后，应用会静默消失、不留任何信息。
    """
    candidates = data_dir_candidates()
    for candidate in candidates:
        if _try_writable(candidate):
            return candidate
    return candidates[-1]


# ── 运行区目录 ─────────────────────────────────────────

# 运行区子目录。**只建目录**：不注册、不复制、不写任何索引 / 注册表文件。
# 依据 docs/ARCHITECTURE.md §4 约定 1 ——「目录即真相，配置即挂载声明」，
# 不引入第三样中间状态（否则 git 一改，中间状态就陈旧了，又得加失效检测）。
_RUNTIME_SUBDIRS = ("procedural_memory", "procedural_memory/quarantine")


def ensure_runtime_dirs(data_dir: Path | None = None) -> list[Path]:
    """建出运行区目录，返回成功创建的列表。

    **只建目录**：不注册、不复制、不写索引文件。
    永不抛异常（与 `_try_writable` 同一条纪律）：单个目录建不出来就跳过它，
    调用方据返回列表自行判断（例如记一条日志）。

    本函数**不导入 config / 第三方包** —— 可被 config 在模块顶层安全调用。
    """
    data_dir = data_dir or app_data_dir()
    created: list[Path] = []
    for rel in _RUNTIME_SUBDIRS:
        target = data_dir / rel
        try:
            target.mkdir(parents=True, exist_ok=True)
            created.append(target)
        except OSError:
            continue
    return created


# ── 临时目录重定向 ─────────────────────────────────────


def ensure_writable_temp(data_dir: Path | None = None) -> Path:
    """把临时目录相关环境变量指到**确认可写**的位置，返回最终使用的目录。

    幂等：可以重复调用。现有 `%TEMP%` 确实可写时**不覆盖**它，只补齐
    SQLite 与 matplotlib 需要的变量。

    背景见模块 docstring 第 1、2、3 条 —— 这是 chroma `code 14` 的唯一解。
    """
    data_dir = data_dir or app_data_dir()

    # matplotlib：固定放在数据目录下，避免每次启动重建字体缓存
    mpl_dir = data_dir / _MPL_SUBDIR
    if _try_writable(mpl_dir):
        os.environ["MPLCONFIGDIR"] = str(mpl_dir)

    for var in ("TEMP", "TMP"):
        current = os.environ.get(var)
        if current and _try_writable(Path(current)):
            # 系统临时目录本来就能用：不折腾，只补 SQLite 需要的变量
            os.environ.setdefault("TMPDIR", current)
            os.environ.setdefault("SQLITE_TMPDIR", current)
            return Path(current)

    target = data_dir / _TEMP_SUBDIR
    if not _try_writable(target):
        target = data_dir          # 兜底：数据目录本身（已知可写）
    for var in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR"):
        os.environ[var] = str(target)
    return target


# ── tiktoken 编码缓存 ─────────────────────────────────
#
# ★★★ 为什么必须显式钉住（2026-10-20 修）：
#
#   `tiktoken.get_encoding("cl100k_base")` 要读一份 1.6 MB 的 BPE 词表。
#   它自己的查找顺序是：
#       TIKTOKEN_CACHE_DIR → DATA_GYM_CACHE_DIR → tempfile.gettempdir()/data-gym-cache
#   最后那条**看起来**没问题，但 `tempfile.gettempdir()` 在本机会退化到
#   **当前工作目录**（`%TEMP%` 被安全软件的文件系统过滤驱动拦），
#   而且结果**只算一次就缓存**。
#
#   后果有两个，都是实测踩到的：
#     ① **打包版离线必失败**：包内 cwd 是安装目录，`data-gym-cache/` 没被打包
#        （spec 的 datas 漏了）→ 找不到 → 去
#        `openaipublic.blob.core.windows.net` 下载 → 无网/被拦就抛 SSLError。
#     ② **守卫会 flaky**：哪个测试先 `chdir`，`gettempdir()` 的缓存值就跟着变，
#        于是同一条断言时绿时红（实测 `check_handoff` 4 次里错 3 次）。
#
#   钉死到一个**与 cwd 无关**的位置，两个问题一起消掉。
_TIKTOKEN_SUBDIR = "data-gym-cache"
_TIKTOKEN_FILE = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"   # cl100k_base 的缓存名


def tiktoken_cache_dir(data_dir: Path | None = None) -> Path:
    """返回 tiktoken 词表缓存目录（**不依赖 cwd**）并写入环境变量。

    打包模式：包内 `_MEIPASS/data-gym-cache`（随 spec 的 datas 分发，离线可用）。
    开发模式：项目根 `data-gym-cache/`。

    ★ 幂等；调用方已显式设置 `TIKTOKEN_CACHE_DIR` 时**尊重它**。
    """
    explicit = os.environ.get("TIKTOKEN_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)

    if IS_FROZEN:
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        cand = base / _TIKTOKEN_SUBDIR
        if cand.is_dir():
            os.environ["TIKTOKEN_CACHE_DIR"] = str(cand)
            return cand
        # 包内没有 → 落到数据目录（可写，首次调用时会下载并缓存）
        cand = (data_dir or app_data_dir()) / _TIKTOKEN_SUBDIR
        cand.mkdir(parents=True, exist_ok=True)
        os.environ["TIKTOKEN_CACHE_DIR"] = str(cand)
        return cand

    cand = Path(__file__).resolve().parent / _TIKTOKEN_SUBDIR
    if cand.is_dir():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(cand)
        return cand
    cand.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cand)
    return cand


def tiktoken_cache_ready(data_dir: Path | None = None) -> bool:
    """词表是否已缓存（已缓存 = 运行期不需要联网）。"""
    d = tiktoken_cache_dir(data_dir)
    return (d / _TIKTOKEN_FILE).is_file()


# ── .env 查找 ─────────────────────────────────────────


def env_file_candidates(data_dir: Path | None = None) -> list[Path]:
    """`.env` 的查找位置，按优先级排列。

    为什么要找多个位置：`app_data_dir()` 在 `%APPDATA%` 不可写时会**降级**到
    exe 同级目录，而用户的 `.env` 通常还留在 `%APPDATA%/CottonAgent/` 里。
    写不进去但**读得到** —— 所以降级绝不该等于丢配置。

    顺序上数据目录排第一：用户一旦在降级目录里保存过设置，那份就是权威配置，
    不会被更早的只读副本盖回去。
    """
    ordered: list[Path] = []
    if data_dir is not None:
        ordered.append(data_dir / ".env")
    ordered.extend(c / ".env" for c in data_dir_candidates())
    if IS_FROZEN:
        ordered.append(Path(sys.executable).resolve().parent / ".env")
    ordered.append(Path(__file__).resolve().parent / ".env")   # 开发模式：项目根

    seen: set[str] = set()
    unique: list[Path] = []
    for path in ordered:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_env_file(data_dir: Path | None = None) -> Path | None:
    """返回第一个真实存在的 `.env`；一个都没有返回 None。"""
    for path in env_file_candidates(data_dir):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None