# -*- coding: utf-8 -*-
"""用户私有记忆：L4 实体记忆 + L3 摘要向量化。

【为什么从 KnowledgeBase 拆出来（S1）】
  公共知识库与用户私有记忆是两种**生命周期完全不同**的数据：

  | | 公共知识库（KnowledgeBase） | 用户私有记忆（UserMemoryStore） |
  |---|---|---|
  | 来源 | `data/` 下的公开农技文档 | 用户在对话中产生的实体与摘要 |
  | 读写 | **只读**（仅构建/同步时写） | **可写**（每轮对话都可能写） |
  | 可重建 | ✅ 删了可从 `data/` 重建 | ❌ 删了就永久丢失 |
  | 可否共享 | ✅ 多会话只读共享安全 | ❌ 必须按用户隔离 |

  拆开前两者共处一个类，导致"共享知识库"与"隔离用户记忆"这两个相反的要求
  互相纠缠（见 ROADMAP 短板 #17/#18）。

【写入方约定（重要）】
  只有**拥有该用户数据**的引擎才应实例化**持久化**的 UserMemoryStore。
  · 桌面端 → 生产路径（唯一正常写入方）
  · Web    → 内存态 / 独立作用域（不得写生产库）
  · 评测   → 沙箱目录（不得写生产库）
  详见 ROADMAP「用户数据隔离 S2」。

【集合】
  · `user_profile`       L4 画像库（稳定属性 / 偏好 / 禁忌）
  · `project_facts`      L4 项目事实库（某一生产季的事实）
  · `session_summaries`  L3 摘要向量化（可按语义检索历史情节）
  · `user_memory`        **旧单集合，仅作未迁移库的兼容层**（见 `UserMemoryStore.__init__`）
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import chromadb

# ── 当前方案：硅基流动 API Embedding ──
from langchain_openai import OpenAIEmbeddings

from config import AppConfig
from core.memory_scope import (
    PROFILE_COLLECTION,
    PROJECT,
    PROJECT_COLLECTION,
    classify_scope,
)

L4_CATEGORIES = ("fact", "preference", "episodic")

# L4 长期记忆容器按**作用域**分库（详见 docs/PROCEDURAL_MEMORY.md）：
#   · 画像库     user_profile  —— 关于"这个人"的稳定属性与偏好（锁死单用户全局）
#   · 项目事实库 project_facts —— 关于"某个生产季"的事实（锁死单项目沙箱）
#   · 技能结晶档 procedural_memory/index.jsonl —— 见 core/procedural_memory.py
#     （**本机私有**；随包分发的是手写技能 skills/，见 docs/ARCHITECTURE.md）
# 旧的单集合 `user_memory` 仅作为**未迁移库的兼容层**保留，见 `__init__` 里的说明。
LEGACY_COLLECTION = "user_memory"


def _utc_now() -> str:
    """统一的 UTC 时间戳（ISO 8601），保证全库格式一致。"""
    return datetime.now(timezone.utc).isoformat()


class NullMemoryStore:
    """空记忆实现：接口与 UserMemoryStore 完全一致，但读写全为 no-op。

    【用途】没有用户身份的调用方（Web 服务多访客场景，见 ROADMAP S2）。

    【为什么是"不记忆"而不是"记在别处"】
      · L4 的价值是**跨会话**长期记忆。Web 访客既无跨会话身份，
        同一会话内的记忆又与 L2 对话历史完全重复 → 留着只白花 Embedding。
      · Web 的 LLM Key 由访客自备，L4 worker 每轮会发起抽取 LLM 调用
        → 禁用即省访客的钱与延迟。
      · 因此正确行为是"不记忆"，而非"记到隔离目录"。
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    @property
    def db_path(self) -> str:
        return "<null: 该引擎不拥有用户数据>"

    def counts(self) -> dict[str, int]:
        return {"user_memory": 0, "session_summaries": 0}

    def counts_split(self) -> dict[str, int]:
        return {}

    def l4_collections(self) -> list:
        return []

    def clear_all_l4(self) -> int:
        return 0

    def clear_all_summaries(self) -> int:
        return 0

    def asserted_at(self, doc_id: str) -> str:
        return ""

    # ---- L4 ----

    def add_l4_memory(self, fact: str, category: str = "fact") -> str:
        return ""

    def search_l4_for_conflict(
        self, fact: str, threshold: float = 0.79,
    ) -> tuple[str | None, str | None]:
        return None, None

    def search_l4_candidates(
        self, fact: str, k: int = 5, *, with_time: bool = False,
    ) -> list:
        """与 `UserMemoryStore` **同名同签名**（接口一致性由守卫断言，不靠约定）。

        ⚠️ 本方法曾长期缺失：`resolve_memory_fact()` 第一句就是
        `self.memory.search_l4_candidates(...)`，Web 场景下 `self.memory` 是本类，
        因此只要 L4 worker 抽到一条事实就抛 `AttributeError`，被 worker 的
        `except Exception` 吞掉后打印一行 —— **不影响数据安全（异常发生在落库之前，
        且本来就不打算写），但"接口一致"这一约定此前没有任何断言守卫**。
        现在由 `tests/check_memory_split.py` 的接口一致性断言守住。
        """
        return []

    def update_l4_memory(self, doc_id: str, new_fact: str) -> None:
        return None

    def delete_l4_memory(self, doc_id: str) -> None:
        return None

    def retrieve_l4_memory(
        self, query: str, k: int = 3, category: str | None = None,
    ) -> list[str]:
        return []

    # ---- L3 ----

    def add_session_summary(
        self, session_id: str, text: str, category: str = "summary",
    ) -> str:
        return ""

    def retrieve_session_summaries(self, query: str, k: int = 3) -> list[str]:
        return []

    def clear_session_summaries(self, session_id: str) -> None:
        return None


class UserMemoryStore:
    """用户私有记忆库（L4 实体记忆 + L3 摘要），独立于公共知识库持久化。"""

    def __init__(self, db_path: str | None = None) -> None:
        """初始化记忆库。

        Args:
            db_path: 记忆库目录。None 时使用 AppConfig.USER_MEMORY_DB_PATH
                     （生产路径）。传入其它路径即可得到**隔离作用域**
                     （Web 内存态 / 评测沙箱）。
        """
        self._db_path = str(db_path or AppConfig.USER_MEMORY_DB_PATH)

        self._embeddings = OpenAIEmbeddings(
            api_key=AppConfig.EMBEDDING_API_KEY,
            base_url=AppConfig.EMBEDDING_BASE_URL,
            model=AppConfig.EMBEDDING_MODEL,
            check_embedding_ctx_length=False,
        )

        self._chroma_client = chromadb.PersistentClient(path=self._db_path)

        # ---- L4：按作用域分离的两个库 ----
        self._profile_collection = self._chroma_client.get_or_create_collection(
            name=PROFILE_COLLECTION,
        )
        self._project_collection = self._chroma_client.get_or_create_collection(
            name=PROJECT_COLLECTION,
        )

        # ---- 兼容未迁移的旧库 ----
        # 条件：两个新库都为空，而旧的 `user_memory` 有数据 → 沿用旧集合。
        # 用途：① 评测 fixture（S0 冻结快照，仍是单集合布局）
        #       ② 尚未迁移的安装 —— 让升级不必强制重建记忆库。
        # 生产库已迁移（user_profile/project_facts 非空），因此**不会**走这条分支。
        self._legacy_collection = None
        try:
            old = self._chroma_client.get_collection(LEGACY_COLLECTION)
            if (
                old.count()
                and not self._profile_collection.count()
                and not self._project_collection.count()
            ):
                self._legacy_collection = old
        except Exception:
            pass

        # ---- L3 摘要向量化（同一客户端下的独立 Collection） ----
        self._summary_collection = self._chroma_client.get_or_create_collection(
            name="session_summaries",
        )

    # ------------------------------------------------------------------
    # 只读辅助
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> str:
        """当前记忆库目录（供日志与隔离验证使用）。"""
        return self._db_path

    def l4_collections(self) -> list:
        """当前生效的 L4 库列表（旧库兼容模式下只有一个旧集合）。

        ⚠️ 提供给清空沙箱之类的操作用 —— 守卫**不应**再去摸 `_xxx_collection`
        这类私有属性：库的布局会随作用域拆分而变，私有属性名不是稳定契约。
        """
        if self._legacy_collection is not None:
            return [self._legacy_collection]
        return [self._profile_collection, self._project_collection]

    def clear_all_l4(self) -> int:
        """清空全部 L4 库，返回删除条数（冒烟守卫用它准备干净沙箱）。"""
        removed = 0
        for col in self.l4_collections():
            ids = col.get(include=[]).get("ids") or []
            if ids:
                col.delete(ids=ids)
                removed += len(ids)
        return removed

    def _l4_collection_for(self, fact: str):
        """按事实内容决定写入哪个库（旧库兼容模式下始终写旧库）。"""
        if self._legacy_collection is not None:
            return self._legacy_collection
        return (
            self._project_collection
            if classify_scope(fact) == PROJECT
            else self._profile_collection
        )

    def _find_l4(self, doc_id: str):
        """定位某条 L4 记忆所在的库；找不到返回 None。"""
        for col in self.l4_collections():
            if col.get(ids=[doc_id], include=[]).get("ids"):
                return col
        return None

    def asserted_at(self, doc_id: str) -> str:
        """取某条记忆的**断言时间**（用户说出它的时间）。

        优先 `asserted_at`；缺失时回退 `updated_at` / `timestamp`（旧 schema）。
        用途：冲突消解时把时间作为**排序与提示信号**，而**不是**硬判据 ——
        "时间更新"不等于"事实更对"（`计划扩种到200亩` 就比 `有100亩` 新）。
        """
        col = self._find_l4(doc_id)
        if col is None:
            return ""
        got = col.get(ids=[doc_id], include=["metadatas"])
        md = ((got.get("metadatas") or [{}])[0]) or {}
        return str(
            md.get("asserted_at") or md.get("updated_at") or md.get("timestamp") or ""
        )

    def counts(self) -> dict[str, int]:
        """各集合当前条目数（供冒烟守卫与隔离验证使用）。

        ⚠️ `user_memory` 是**两个 L4 库之和**。键名与含义刻意保持不变：
        `tests/smoke_memory.py` 对返回值做**精确字典相等**断言，改形状会打破既有守卫。
        要看拆分明细请用 `counts_split()`。
        """
        return {
            "user_memory": sum(c.count() for c in self.l4_collections()),
            "session_summaries": self._summary_collection.count(),
        }

    def counts_split(self) -> dict[str, int]:
        """L4 按库拆分后的条目数（供拆分相关守卫与诊断使用）。"""
        if self._legacy_collection is not None:
            return {"legacy(user_memory)": self._legacy_collection.count()}
        return {
            PROFILE_COLLECTION: self._profile_collection.count(),
            PROJECT_COLLECTION: self._project_collection.count(),
        }

    # ---- L4 实体记忆 (添加 / 查询 / 冲突消解) ----

    def add_l4_memory(self, fact: str, category: str = "fact") -> str:
        """存入单条实体事实，自动生成 UUID 和 timestamp 元数据。

        Args:
            fact: 一条实体事实陈述句。
            category: 事实类别 — "fact"(客观事实) / "preference"(用户偏好)
                      / "episodic"(事件经历)。

        Returns:
            新创建的文档 ID。
        """
        if category not in L4_CATEGORIES:
            category = "fact"
        doc_id = str(uuid.uuid4())
        embedding = self._embeddings.embed_documents([fact])
        now = _utc_now()
        self._l4_collection_for(fact).add(
            embeddings=embedding,
            documents=[fact],
            metadatas=[{
                "source": "L4_realtime",
                "category": category,
                # `asserted_at` = **用户说出这条事实的时间**：写入即定、永不改写。
                # 冲突消解需要的是它，而不是"记录什么时候被改过"（`updated_at`）。
                # 两者混用会让归并/修订过的旧内容看起来像新事实。
                "asserted_at": now,
                "asserted_at_source": "write_time",
                "updated_at": now,
            }],
            ids=[doc_id],
        )
        return doc_id

    def search_l4_candidates(
        self, fact: str, k: int = 5, *, with_time: bool = False,
    ) -> list[tuple]:
        """返回与新事实最相关的 **top-K** 旧记忆，含换算后的余弦相似度。

        这是**生产写入路径应当使用的接口**。与 `search_l4_for_conflict` 的关键差别：
        **不再用硬阈值把候选挡在门外**——旧实现用 `1/(1+L2²) >= 0.7`（等价余弦
        0.786）做闸门，而"同一属性但取值变了"的情形（品种更换 0.51、计划取消 0.51、
        放弃扩种 0.42）**永远达不到这条线**，于是连冲突判定都不会发生、直接新增
        （ROADMAP 短板 #22，记忆膨胀的结构性主因）。

        这里相似度**降级为排序信号**；"是否同一属性"交给 LLM 判断。

        Args:
            fact: 候选事实文本。
            k: 返回候选条数。
            with_time: 为 True 时每项**多带第 4 个元素** —— 该条的断言时间
                （`asserted_at`，缺失时回退 `updated_at` / `timestamp`）。
                冲突消解用它作**参考信号**（见 `L4_CONFLICT_PROMPT` 的日期段）；
                默认 False，既有调用方行为不变。

        Returns:
            `with_time=False`：`[(doc_id, doc_text, cosine), ...]`
            `with_time=True` ：`[(doc_id, doc_text, cosine, asserted_at), ...]`
            均按相似度降序；库为空时返回 []。

        Note:
            余弦换算 `cos = 1 - L2²/2` 假设**向量已归一化**（bge 系列满足，实测
            ||e|| = 1.0000）。L2² 与余弦在归一化向量上严格单调等价，故排序不受影响。
        """
        query_embedding = self._embeddings.embed_query(fact)
        # 只在本条事实**将要去**的那个库里找候选：画像事实不该与某季的项目事实互相判定
        results = self._l4_collection_for(fact).query(
            query_embeddings=[query_embedding],
            n_results=k,
            include=["documents", "distances", "metadatas"],
        )
        ids = (results.get("ids") or [[]])[0]
        docs = (results.get("documents") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]

        mds = (results.get("metadatas") or [[]])[0]
        out: list[tuple] = []
        for i, doc in enumerate(docs):
            d = float(dists[i])                      # ChromaDB 默认空间 = L2 平方欧氏距离
            cos = max(-1.0, min(1.0, 1.0 - d / 2.0))
            if not with_time:
                out.append((ids[i], doc, cos))
                continue
            md = (mds[i] if i < len(mds) else {}) or {}
            ts = str(
                md.get("asserted_at") or md.get("updated_at") or md.get("timestamp") or ""
            )
            out.append((ids[i], doc, cos, ts))
        return out

    def search_l4_for_conflict(
        self, fact: str, threshold: float = 0.79,
    ) -> tuple[str | None, str | None]:
        """语义检索最相似的**单条**旧记忆（按余弦相似度判定）。

        ⚠️ **兼容保留的简化接口**，只回最相似的一条，且带硬阈值。
           生产写入路径请改用 `search_l4_candidates()` —— 硬阈值会把"同属性但取值
           变了"的情形挡在门外（短板 #22）。

        阈值语义修正说明：本方法原先比较的是 `1/(1+L2²) >= 0.7`，而集合未声明
        `hnsw:space`，ChromaDB 默认是 **L2 平方欧氏距离**（不是此处旧注释误写的
        "余弦距离"）。在归一化向量上 `L2² = 2(1-cos)`，故原阈值实际等价于
        **余弦 ≥ 0.7857**；现直接以余弦表达（默认 0.79），行为等价但语义清晰。

        Returns:
            (doc_id, doc_text)；未达阈值时 (None, None)。
        """
        cands = self.search_l4_candidates(fact, k=1)
        if cands and cands[0][2] >= threshold:
            return cands[0][0], cands[0][1]
        return None, None

    def update_l4_memory(self, doc_id: str, new_fact: str) -> None:
        """根据 ID 更新已有 L4 记忆的文本内容（自动定位所在库）。

        Args:
            doc_id: 目标记忆的 ChromaDB ID。
            new_fact: 新的内容文本。

        Note:
            写回时**保留原有 metadata**（category / source）—— 旧实现只写
            `updated_at`，等于把这两项丢掉；而 `category` 是检索过滤的依据。
        """
        col = self._find_l4(doc_id)
        if col is None:
            return
        got = col.get(ids=[doc_id], include=["metadatas"])
        old_md = dict(((got.get("metadatas") or [{}])[0]) or {})
        # `asserted_at` **永不被改写**；缺失时用更新前的时间近似回填并标注来源，
        # 免得以后把它当成精确值用。
        if not old_md.get("asserted_at"):
            old_md["asserted_at"] = (
                old_md.get("updated_at") or old_md.get("timestamp") or ""
            )
            old_md["asserted_at_source"] = "backfill_approx"
        old_md["updated_at"] = _utc_now()
        new_embedding = self._embeddings.embed_documents([new_fact])
        col.update(
            ids=[doc_id],
            embeddings=new_embedding,
            documents=[new_fact],
            metadatas=[old_md],
        )

    def delete_l4_memory(self, doc_id: str) -> None:
        """根据 ID 删除过期 L4 记忆（自动定位所在库）。

        Args:
            doc_id: 目标记忆的 ChromaDB ID。
        """
        col = self._find_l4(doc_id)
        if col is not None:
            col.delete(ids=[doc_id])

    def retrieve_l4_memory(
        self, query: str, k: int = 3, category: str | None = None,
    ) -> list[str]:
        """语义检索与查询最相关的 L4 实体记忆（**跨两个库合并后重排**）。

        Args:
            query: 查询文本。
            k: 返回数量，默认 3。
            category: 可选类别过滤 — "fact"/"preference"/"episodic"；
                      None 表示不过滤。

        Returns:
            相关事实字符串列表。

        Note:
            两个库共用同一嵌入空间，因此 L2 距离**可比**，合并后统一升序取前 k。
            旧实现只查单一集合，拆分后若不合并就会漏掉另一半记忆。
        """
        query_embedding = self._embeddings.embed_query(query)
        merged: list[tuple[float, str]] = []
        for col in self.l4_collections():
            kwargs: dict = {
                "query_embeddings": [query_embedding],
                "n_results": k,
                "include": ["documents", "distances"],
            }
            if category in L4_CATEGORIES:
                kwargs["where"] = {"category": category}
            try:
                results = col.query(**kwargs)
            except Exception:
                continue
            docs = (results.get("documents") or [[]])[0]
            dists = (results.get("distances") or [[]])[0]
            for doc, dist in zip(docs, dists):
                merged.append((float(dist), doc))
        merged.sort(key=lambda x: x[0])
        return [doc for _dist, doc in merged[:k]]

    # ---- L3 摘要向量化 (可语义检索的历史情节记忆) ----

    def add_session_summary(
        self, session_id: str, text: str, category: str = "summary",
    ) -> str:
        """将一段会话摘要向量化存入 L3 摘要库。

        每段摘要作为独立文档保存（增量式），带 session_id / category /
        timestamp 元数据，可通过语义检索"翻旧账"。

        Args:
            session_id: 所属会话 ID。
            text: 摘要文本。
            category: 摘要类别，默认 "summary"。

        Returns:
            新创建的文档 ID。
        """
        doc_id = str(uuid.uuid4())
        embedding = self._embeddings.embed_documents([text])
        self._summary_collection.add(
            embeddings=embedding,
            documents=[text],
            metadatas=[{
                "session_id": session_id,
                "category": category,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
            ids=[doc_id],
        )
        return doc_id

    def retrieve_session_summaries(self, query: str, k: int = 3) -> list[str]:
        """语义检索与查询最相关的历史摘要段。

        Args:
            query: 查询文本。
            k: 返回数量，默认 3。

        Returns:
            相关摘要文本列表。
        """
        query_embedding = self._embeddings.embed_query(query)
        results = self._summary_collection.query(
            query_embeddings=[query_embedding],
            n_results=k,
        )
        docs: list = results.get("documents", [[]])
        if docs and docs[0]:
            return docs[0]
        return []

    def clear_session_summaries(self, session_id: str) -> None:
        """删除指定会话的全部向量化摘要（会话删除时调用）。"""
        got = self._summary_collection.get(where={"session_id": session_id})
        ids: list = got.get("ids") or []
        if ids:
            self._summary_collection.delete(ids=ids)

    def clear_all_summaries(self) -> int:
        """清空全部 L3 摘要，返回删除条数（冒烟守卫用它准备干净沙箱）。"""
        ids: list = self._summary_collection.get(include=[]).get("ids") or []
        if ids:
            self._summary_collection.delete(ids=ids)
        return len(ids)
