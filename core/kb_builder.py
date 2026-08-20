"""
知识库自动构建管理器

职责：
  - 文档指纹计算与持久化（检测 data/ 文档增删改）
  - Embedding API 可用性验证（带 24h 缓存）
  - 全量重建知识库（调用线程内执行，GUI 侧负责后台线程调度）

设计约束：
  - 知识库增删改由开发者控制（data/ 为只读资产，打包后位于 _MEIPASS）
  - 用户机器上首次启动自动构建；文档指纹变化（软件更新）自动重建
  - 构建失败不影响对话：4 个本地数据工具不依赖知识库
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from config import AppConfig, app_data_dir

log = logging.getLogger("kb")

# 支持的文档扩展名（与 knowledge_base 一致）
DOC_EXTENSIONS = {".md", ".pdf", ".txt"}

# Embedding 验证缓存文件（APPDATA/embedding_check.json）
_EMBEDDING_CHECK_FILE = "embedding_check.json"
_EMBEDDING_CACHE_TTL = 24 * 3600  # 24 小时内不重复验证


# ── 文档指纹 ───────────────────────────────────────────

def _fingerprint_file() -> Path:
    return app_data_dir() / "kb_fingerprint.json"


def _model_signature() -> str:
    """Embedding 模型签名：URL + 模型名。

    换模型/换服务商会改变向量维度，必须触发重建（否则检索报维度错误）。
    """
    return f"{AppConfig.EMBEDDING_BASE_URL}|{AppConfig.EMBEDDING_MODEL}"


def compute_fingerprint(data_dir: Path | None = None) -> str:
    """计算知识库指纹 = 文档哈希 + Embedding 模型签名。

    文档增删改、或更换 Embedding 模型，都会使指纹变化 → 触发重建。
    """
    data_dir = data_dir or Path(AppConfig.DATA_DIR)
    h = hashlib.md5()
    if data_dir.exists():
        for p in sorted(data_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in DOC_EXTENSIONS:
                try:
                    st = p.stat()
                    h.update(f"{p.relative_to(data_dir)}|{st.st_size}|{int(st.st_mtime)}"
                             .encode("utf-8"))
                except OSError:
                    continue
    # 模型签名：换模型必须重建（向量维度不兼容）
    h.update(f"|{_model_signature()}".encode("utf-8"))
    return h.hexdigest()


def load_fingerprint() -> str | None:
    """读取上次构建的指纹；无记录返回 None。"""
    fp_file = _fingerprint_file()
    if not fp_file.exists():
        return None
    try:
        return json.loads(fp_file.read_text("utf-8")).get("fingerprint")
    except (OSError, json.JSONDecodeError):
        return None


def save_fingerprint(fp: str) -> None:
    """持久化指纹（含模型签名，用于检测模型切换）。"""
    _fingerprint_file().write_text(
        json.dumps({"fingerprint": fp, "model_sig": _model_signature(),
                    "updated_at": time.time()}, ensure_ascii=False),
        "utf-8",
    )


def model_changed() -> bool:
    """Embedding 模型是否与上次构建时不同（含旧格式无记录的情况）。"""
    fp_file = _fingerprint_file()
    if not fp_file.exists():
        return False
    try:
        saved = json.loads(fp_file.read_text("utf-8")).get("model_sig")
    except (OSError, json.JSONDecodeError):
        saved = None
    # 旧格式无 model_sig → 保守视为已变化，触发一次重建并写入新格式
    return saved != _model_signature()


def needs_rebuild() -> bool:
    """是否需要（重新）构建知识库：无指纹记录、文档变化或模型变化。"""
    saved = load_fingerprint()
    if saved is None:
        return True
    return compute_fingerprint() != saved


# ── Embedding API 验证 ────────────────────────────────

def _check_file() -> Path:
    return app_data_dir() / _EMBEDDING_CHECK_FILE


def _load_embedding_check() -> dict:
    try:
        return json.loads(_check_file().read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def verify_embedding(force: bool = False) -> tuple[bool, str]:
    """验证 Embedding API 是否可用（试调一次 embed_query）。

    带 24h 缓存：成功结果 24 小时内不重复调用 API。

    Returns:
        (ok, message): ok=False 时 message 说明原因（供 UI 降级提示）。
    """
    if not AppConfig.EMBEDDING_API_KEY.strip():
        return False, "未配置 Embedding API Key，请先在设置中填写"

    if not force:
        cached = _load_embedding_check()
        if cached.get("ok") and time.time() - cached.get("ts", 0) < _EMBEDDING_CACHE_TTL:
            return True, "Embedding API 可用（缓存）"

    try:
        from langchain_openai import OpenAIEmbeddings

        emb = OpenAIEmbeddings(
            api_key=AppConfig.EMBEDDING_API_KEY,
            base_url=AppConfig.EMBEDDING_BASE_URL,
            model=AppConfig.EMBEDDING_MODEL,
            check_embedding_ctx_length=False,
        )
        emb.embed_query("知识库构建验证")
        _check_file().write_text(
            json.dumps({"ok": True, "ts": time.time()}, ensure_ascii=False), "utf-8",
        )
        return True, "Embedding API 可用"
    except Exception as e:
        return False, f"Embedding API 验证失败: {e}"


# ── 知识库构建 ─────────────────────────────────────────

def rebuild_knowledge_base() -> tuple[bool, str]:
    """全量重建知识库（在调用线程内执行）。

    若 Embedding 模型发生变化，同步重建 L4 实体记忆
    （向量维度不兼容，旧记忆无法检索）。

    Returns:
        (成功与否, 信息文本)。
    """
    try:
        from core.knowledge_base import KnowledgeBase

        # 模型变化 → 清空 L4 记忆库与 L3 摘要库（维度不兼容，保留会检索报错）
        if model_changed():
            try:
                import chromadb

                client = chromadb.PersistentClient(path=AppConfig.USER_MEMORY_DB_PATH)
                client.delete_collection("user_memory")
                client.delete_collection("session_summaries")
                print("[KB] Embedding 模型已更换，L4 记忆库与 L3 摘要库已重建。")
                log.info("Embedding 模型已更换，L4 记忆库与 L3 摘要库已重建。")
            except Exception:
                pass

        kb = KnowledgeBase()
        kb.build_vector_db()
        save_fingerprint(compute_fingerprint())
        return True, "知识库构建完成"
    except Exception as e:
        return False, f"知识库构建失败: {e}"
