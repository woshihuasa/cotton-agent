"""
知识库模块
负责文档加载、文本分割、向量存储与相似度检索。
基于 LangChain + ChromaDB 构建本地 RAG 知识库。

L4 实体记忆：独立 ChromaDB Collection (user_memory),
支持主动实时维护与冲突消解 (ADD / UPDATE / DELETE),
跨会话持久化农事实体信息。

Embedding 方案：
  - 当前：硅基流动 API (BAAI/bge-large-zh-v1.5)，通过 OpenAI 兼容接口调用
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_community.document_loaders import PyMuPDFLoader, TextLoader

# ── 当前方案：硅基流动 API Embedding ──
from langchain_openai import OpenAIEmbeddings


from config import AppConfig


class KnowledgeBase:
    """本地知识库，封装文档向量化存储与 Top-K 语义检索。

    支持 PDF / TXT / MD 格式文档，使用硅基流动 API 生成 Embedding，
    通过 ChromaDB 持久化向量并执行相似度检索。
    """

    def __init__(self) -> None:
        """初始化 Embedding 模型与 ChromaDB 向量存储。"""
        # ── 当前方案：硅基流动 API Embedding ──
        self._embeddings = OpenAIEmbeddings(
            api_key=AppConfig.EMBEDDING_API_KEY,
            base_url=AppConfig.EMBEDDING_BASE_URL,
            model=AppConfig.EMBEDDING_MODEL,
            check_embedding_ctx_length=False,
        )

        # 初始化 ChromaDB 持久化客户端
        self._vector_store = Chroma(
            persist_directory=AppConfig.CHROMA_DB_PATH,
            embedding_function=self._embeddings,
            collection_name="cotton_docs",
        )

        # ---- L4 实体记忆 (独立 Collection, 使用独立路径防止被 RAG 重建误删) ----
        self._chroma_client = chromadb.PersistentClient(
            path=AppConfig.USER_MEMORY_DB_PATH,
        )
        self._memory_collection = self._chroma_client.get_or_create_collection(
            name="user_memory",
        )
        # ---- L3 摘要向量化 (同一客户端下的独立 Collection, 可语义检索历史情节) ----
        self._summary_collection = self._chroma_client.get_or_create_collection(
            name="session_summaries",
        )

    def _load_and_split_documents(self) -> list:
        """遍历数据目录，加载并切分所有支持的文档。

        依次扫描 DATA_DIR 下的 *.pdf、*.txt、*.md 文件，
        使用对应的 Loader 加载后，通过 RecursiveCharacterTextSplitter
        按 chunk_size=500、chunk_overlap=50 进行文本分割。

        Returns:
            切分后的 Document chunk 列表；若无文件可加载则返回空列表。
        """
        data_dir = Path(AppConfig.DATA_DIR)

        # 检查数据目录是否存在
        if not data_dir.exists():
            print(f"[KnowledgeBase] 数据目录不存在：{data_dir.resolve()}")
            print("[KnowledgeBase] 请将 PDF / TXT / MD 文档放入该目录后重试。")
            return []

        documents = []

        # ── 加载 PDF 文件 ──
        for pdf_path in data_dir.glob("**/*.pdf"):
            try:
                loader = PyMuPDFLoader(str(pdf_path))
                documents.extend(loader.load())
                print(f"[KnowledgeBase] 已加载 PDF：{pdf_path.name}")
            except Exception as e:
                print(f"[KnowledgeBase] 加载 PDF 失败 {pdf_path.name}：{e}")

        # ── 加载 TXT 文件 ──
        for txt_path in data_dir.glob("**/*.txt"):
            try:
                loader = TextLoader(str(txt_path), encoding="utf-8")
                documents.extend(loader.load())
                print(f"[KnowledgeBase] 已加载 TXT：{txt_path.name}")
            except Exception as e:
                print(f"[KnowledgeBase] 加载 TXT 失败 {txt_path.name}：{e}")

        # ── 加载 MD 文件 ──
        for md_path in data_dir.glob("**/*.md"):
            try:
                loader = TextLoader(str(md_path), encoding="utf-8")
                documents.extend(loader.load())
                print(f"[KnowledgeBase] 已加载 MD：{md_path.name}")
            except Exception as e:
                print(f"[KnowledgeBase] 加载 MD 失败 {md_path.name}：{e}")

        # 无文档时的友好提示
        if not documents:
            print("[KnowledgeBase] 未找到任何可加载的文档（PDF/TXT/MD）。")
            return []

        # ── 文本分割 ──
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            separators=["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", " ", ""],
        )
        chunks = text_splitter.split_documents(documents)
        print(f"[KnowledgeBase] 文档切分完成，共 {len(chunks)} 个文本块。")
        return chunks

    def build_vector_db(self) -> None:
        """构建 / 重建 RAG 文档向量库。

        使用独立 collection name (cotton_docs)，
        不触及同一路径下的 L4 数据。
        每次重建前删除旧 collection 避免重复追加。
        """
        # 清空旧 RAG 文档 collection（不影响 L4 user_memory）
        rag_client = chromadb.PersistentClient(path=AppConfig.CHROMA_DB_PATH)
        try:
            rag_client.delete_collection("cotton_docs")
            print("[KnowledgeBase] 已清空旧 RAG 文档库。")
        except Exception:
            pass

        print("[KnowledgeBase] 开始构建 RAG 文档向量数据库 ...")
        chunks = self._load_and_split_documents()

        if not chunks:
            print("[KnowledgeBase] 无文档块可写入，向量库构建中止。")
            return

        print(f"[KnowledgeBase] 正在存入 {len(chunks)} 个文本块 (Collection: cotton_docs) ...")
        self._vector_store = Chroma.from_documents(
            documents=chunks,
            embedding=self._embeddings,
            persist_directory=AppConfig.CHROMA_DB_PATH,
            collection_name="cotton_docs",
        )
        print("[KnowledgeBase] RAG 文档向量库构建完成。")

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
        if category not in ("fact", "preference", "episodic"):
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

    def search_l4_for_conflict(
        self, fact: str, threshold: float = 0.7,
    ) -> tuple[str | None, str | None]:
        """语义检索 L4 记忆库，检测与新事实冲突的旧记忆。

        ChromaDB 默认使用余弦距离 (distance), 值越小越相似。
        转换为相似度: sim = 1 / (1 + distance)。

        Args:
            fact: 候选事实文本。
            threshold: 相似度阈值，>= 此值视为冲突。

        Returns:
            (doc_id, doc_text) — 冲突记忆的 ID 和文本；
            无冲突时返回 (None, None)。
        """
        query_embedding = self._embeddings.embed_query(fact)
        results = self._memory_collection.query(
            query_embeddings=[query_embedding],
            n_results=1,
            include=["documents", "distances"],
        )
        distances: list = results.get("distances", [[]])
        docs: list = results.get("documents", [[]])
        ids: list = results.get("ids", [[]])

        if not distances[0] or not docs[0]:
            return None, None

        distance = distances[0][0]
        similarity = 1.0 / (1.0 + distance)

        if similarity >= threshold:
            return ids[0][0], docs[0][0]
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
        if category in ("fact", "preference", "episodic"):
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

    def retrieve_context(self, query: str, k: int = 3) -> str:
        """语义检索与查询最相关的 Top-K 文档片段。

        Args:
            query: 查询文本。
            k: 返回的文档块数量，默认 3。

        Returns:
            拼接后的上下文字符串，各片段以双换行分隔；
            若无结果则返回空字符串。
        """
        docs = self._vector_store.similarity_search(query, k=k)
        if not docs:
            print("[KnowledgeBase] 未检索到相关文档。")
            return ""
        contexts = [doc.page_content for doc in docs]
        return "\n\n".join(contexts)
