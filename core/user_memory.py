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
  · `user_memory`        L4 实体记忆（fact / preference / episodic）
  · `session_summaries`  L3 摘要向量化（可按语义检索历史情节）
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import chromadb

# ── 当前方案：硅基流动 API Embedding ──
from langchain_openai import OpenAIEmbeddings

from config import AppConfig

L4_CATEGORIES = ("fact", "preference", "episodic")


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

    # ---- L4 ----

    def add_l4_memory(self, fact: str, category: str = "fact") -> str:
        return ""

    def search_l4_for_conflict(
        self, fact: str, threshold: float = 0.7,
    ) -> tuple[str | None, str | None]:
        return None, None

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

        # ---- L4 实体记忆 ----
        self._memory_collection = self._chroma_client.get_or_create_collection(
            name="user_memory",
        )
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

    def counts(self) -> dict[str, int]:
        """各集合当前条目数（供冒烟守卫与隔离验证使用）。"""
        return {
            "user_memory": self._memory_collection.count(),
            "session_summaries": self._summary_collection.count(),
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
        self._memory_collection.add(
            embeddings=embedding,
            documents=[fact],
            metadatas=[{
                "source": "L4_realtime",
                "category": category,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }],
            ids=[doc_id],
        )
        return doc_id

    def search_l4_candidates(
        self, fact: str, k: int = 5,
    ) -> list[tuple[str, str, float]]:
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

        Returns:
            [(doc_id, doc_text, cosine), ...]，按相似度降序；库为空时返回 []。

        Note:
            余弦换算 `cos = 1 - L2²/2` 假设**向量已归一化**（bge 系列满足，实测
            ||e|| = 1.0000）。L2² 与余弦在归一化向量上严格单调等价，故排序不受影响。
        """
        query_embedding = self._embeddings.embed_query(fact)
        results = self._memory_collection.query(
            query_embeddings=[query_embedding],
            n_results=k,
            include=["documents", "distances"],
        )
        ids = (results.get("ids") or [[]])[0]
        docs = (results.get("documents") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]

        out: list[tuple[str, str, float]] = []
        for i, doc in enumerate(docs):
            d = float(dists[i])                      # ChromaDB 默认空间 = L2 平方欧氏距离
            cos = max(-1.0, min(1.0, 1.0 - d / 2.0))
            out.append((ids[i], doc, cos))
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
        """根据 ID 更新已有 L4 记忆的文本内容。

        Args:
            doc_id: 目标记忆的 ChromaDB ID。
            new_fact: 新的内容文本。
        """
        new_embedding = self._embeddings.embed_documents([new_fact])
        self._memory_collection.update(
            ids=[doc_id],
            embeddings=new_embedding,
            documents=[new_fact],
            metadatas=[{"updated_at": datetime.now(timezone.utc).isoformat()}],
        )

    def delete_l4_memory(self, doc_id: str) -> None:
        """根据 ID 删除过期 L4 记忆。

        Args:
            doc_id: 目标记忆的 ChromaDB ID。
        """
        self._memory_collection.delete(ids=[doc_id])

    def retrieve_l4_memory(
        self, query: str, k: int = 3, category: str | None = None,
    ) -> list[str]:
        """语义检索与查询最相关的 L4 实体记忆。

        Args:
            query: 查询文本。
            k: 返回数量，默认 3。
            category: 可选类别过滤 — "fact"/"preference"/"episodic"；
                      None 表示不过滤。

        Returns:
            相关事实字符串列表。
        """
        query_embedding = self._embeddings.embed_query(query)
        kwargs: dict = {
            "query_embeddings": [query_embedding],
            "n_results": k,
        }
        if category in L4_CATEGORIES:
            kwargs["where"] = {"category": category}
        results = self._memory_collection.query(**kwargs)
        docs: list = results.get("documents", [[]])
        if docs and docs[0]:
            return docs[0]
        return []

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
