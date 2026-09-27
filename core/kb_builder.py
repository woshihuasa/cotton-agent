"""
知识库构建与同步管理器

职责：
  - 文档指纹计算与持久化（**内容 SHA256** + Embedding 模型签名）
  - Embedding API 可用性验证（带 24h 缓存）
  - **同步规划**（`plan_sync`）：事前判定 none / incremental / full
  - **增量同步**（`sync_knowledge_base`）：只处理发生变化的文件
  - 全量重建（`rebuild_knowledge_base`，在调用线程内执行）

设计约束：
  - 知识库内容由开发者维护（打包后 data/ 位于 _MEIPASS，用户不可改）
  - 文档更新随版本分发到用户端 → 启动时**自动检测并同步**，无需用户操作
  - 指纹用内容 hash 而非 mtime：打包后临时目录的 mtime 每次都变，会导致误判
  - 同步失败不影响对话：4 个本地数据工具不依赖知识库
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
    """计算知识库指纹 = 各文档**内容 hash** + Embedding 模型签名。

    文档增删改、或更换 Embedding 模型，都会使指纹变化 → 触发重建。

    为什么用内容 hash 而不是 size+mtime：打包后 data/ 位于 _MEIPASS 临时目录，
    **每次启动解压出来的文件 mtime 都是新的**，用 mtime 会每次启动都误判为
    "需要重建"。内容 hash 与文件时间戳无关，开发 / 打包两种模式行为一致。
    """
    from core.knowledge_base import _to_rel, file_sha256

    data_dir = data_dir or Path(AppConfig.DATA_DIR)
    entries: list[str] = []
    if data_dir.exists():
        for p in sorted(data_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in DOC_EXTENSIONS:
                try:
                    entries.append(f"{_to_rel(p)}|{file_sha256(p)}")
                except OSError:
                    continue
    h = hashlib.md5()
    h.update("\n".join(sorted(entries)).encode("utf-8"))
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


# ── 同步规划与增量同步（启动时自动更新用）─────────────

def plan_sync() -> dict:
    """规划知识库同步（**只检测，不写入任何数据**）。

    在动手之前判定"该做什么"，让 UI 能显示对应文案 ——
    "首次建立 / 模型变更"（需全量，约 1 分钟）与"文档变化"（可增量，几秒）
    对用户而言是两种完全不同的等待体验。

    Returns:
        {"mode": "none" | "incremental" | "full", "reason": 说明,
         "added"/"updated"/"removed": 文件级计数（仅 incremental）,
         "total": 文档总数（仅 full 且首次建立时）}
    """
    # ① 嵌入模型变更 → 必须全量（向量维度不兼容）
    if model_changed():
        return {"mode": "full", "reason": "embedding 模型变更"}

    try:
        from core.knowledge_base import KnowledgeBase

        kb = KnowledgeBase()
    except Exception as e:
        log.warning("同步规划失败（知识库不可读）: %s", e)
        return {"mode": "none", "reason": f"知识库不可读: {e}"}

    # ② 库中尚无任何片段 → 首次建立（全量）
    if not kb._indexed_files():
        total = len(kb._scan_files())
        if total == 0:
            return {"mode": "none", "reason": "data/ 内没有可索引的文档"}
        return {"mode": "full", "reason": "首次建立", "total": total}

    # ③ 有差异 → 增量同步
    diff = kb.diff_index()
    if any(diff.values()):
        return {"mode": "incremental", "reason": "文档有变化",
                "added": len(diff["added"]), "updated": len(diff["updated"]),
                "removed": len(diff["removed"])}

    # ④ 一致
    return {"mode": "none", "reason": "已是最新"}


def sync_knowledge_base() -> tuple[bool, str]:
    """增量同步知识库（在调用线程内执行）。

    · 文档增删改 → 只处理变化的文件，不重建整个向量库
    · 嵌入模型变更 / 首次建立 → 回退为全量重建（内含 L4/L3 重置）

    Returns:
        (成功与否, 信息文本)。
    """
    plan = plan_sync()

    if plan["mode"] == "none":
        return True, plan["reason"]

    if plan["mode"] == "full":
        log.info("执行全量重建（原因：%s）", plan["reason"])
        return rebuild_knowledge_base()

    try:
        from core.knowledge_base import KnowledgeBase

        kb = KnowledgeBase()
        result = kb.sync_index()
        save_fingerprint(compute_fingerprint())
        msg = (f"知识库已更新：+{result['chunks_added']} / -{result['chunks_removed']} 片段"
               f"（文件级 {result['added']} 新增 / {result['updated']} 更新 / "
               f"{result['removed']} 删除）")
        log.info("增量同步完成：%s", msg)
        return True, msg
    except Exception as e:
        log.exception("增量同步失败")
        return False, f"知识库更新失败: {e}"
