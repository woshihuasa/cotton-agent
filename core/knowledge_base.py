"""
知识库模块
负责文档加载、文本分割、向量存储、语义检索与精排。
基于 LangChain + ChromaDB 构建本地 RAG 知识库。

检索链路：向量召回 top-N → Rerank 精排（bge-reranker-v2-m3）→ 取 top-k 注入上下文。

增量索引维护：以**内容 hash** 作为变更指纹、以**相对 DATA_DIR 的相对标识**作为 source，
使文档的增 / 删 / 改只需重嵌变化部分（见 diff_index / sync_index / remove_documents）。

用户私有记忆（L4 实体记忆 / L3 摘要向量化）已拆分到 `core/user_memory.py`。
本模块只负责**公共知识库**：`data/` 下的公开文档 → `cotton_docs` 集合（只读、可重建、可共享）。

Embedding 方案：
  - 当前：硅基流动 API (BAAI/bge-large-zh-v1.5)，通过 OpenAI 兼容接口调用
"""

import hashlib
import os
from pathlib import Path

import chromadb
import requests
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_community.document_loaders import PyMuPDFLoader, TextLoader

# ── 当前方案：硅基流动 API Embedding ──
from langchain_openai import OpenAIEmbeddings


from config import AppConfig

def _to_rel(path_or_source: str | Path) -> str:
    """统一为**相对 DATA_DIR 的 posix 相对路径**（如 `新疆_栽培与水肥/x.md`）。

    为什么用相对标识而不是绝对路径：打包后 DATA_DIR 位于 PyInstaller 的
    `_MEIPASS` 临时目录，**每次启动目录名都不同**（_MEIxxxxxx），绝对路径不稳定，
    会导致每次启动都判定"全部文档已变化"，进而反复全量重建。

    兼容三种来源形态：
      · 绝对路径      E:\\...\\data\\新疆_.../x.md
      · 带 data 前缀   data/新疆_.../x.md  或  data\\新疆_...\\x.md
      · 纯相对路径     新疆_.../x.md
    """
    s = str(path_or_source).replace("\\", "/")
    p = Path(s)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(AppConfig.DATA_DIR).resolve()).as_posix()
        except (ValueError, OSError):
            return s                             # 不在 data/ 下（异常情况）→ 原样返回
    if s.lower().startswith("data/"):
        return s[5:]
    return s


def _to_source(path: str | Path) -> str:
    """写入向量库 metadata 的 source：`data/<相对 DATA_DIR 的路径>`（稳定、可读）。"""
    return "data/" + _to_rel(path)


def _abs_path(rel: str | Path) -> Path:
    """由相对标识还原为绝对路径（用于加载文件与计算指纹）。"""
    return Path(AppConfig.DATA_DIR) / _to_rel(rel)


def file_sha256(path: str | Path) -> str:
    """计算文件内容的 SHA256（用于变更检测）。

    以**内容**而非 mtime 作为指纹：文件修改时间可能因复制、checkout、
    脚本写回等原因不变或不可靠，而内容 hash 不会漏检。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


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

        # 用户私有记忆（L4 / L3）不在此处——见 core/user_memory.py
        # 公共知识库与用户记忆使用**不同目录**，避免 RAG 重建误删用户数据

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
        # 写入内容指纹与**相对标识**，与 _load_and_split_files 保持同一格式
        # （否则全量重建后增量检测会失效）
        hash_cache: dict[str, str] = {}
        for c in chunks:
            src = c.metadata.get("source", "")
            if not src:
                continue
            rel = _to_rel(src)                       # → 相对标识（带/不带 data 前缀都能归一）
            c.metadata["source"] = _to_source(rel)    # → "data/<相对路径>"
            if rel not in hash_cache:
                try:
                    hash_cache[rel] = file_sha256(_abs_path(rel))
                except OSError:
                    hash_cache[rel] = ""
            c.metadata["file_hash"] = hash_cache[rel]
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

    # ---- 增量索引维护（增 / 删 / 改，避免全量重建）----

    def _scan_files(self) -> dict[str, tuple[str, str]]:
        """扫描 DATA_DIR，返回 {相对标识: (绝对路径, 内容 SHA256)}。

        键用**相对标识**而非绝对路径：打包后 DATA_DIR 在 _MEIPASS（每次启动目录名
        都不同），绝对路径会导致每次启动都判定"全部文档已变化"。
        """
        data_dir = Path(AppConfig.DATA_DIR)
        if not data_dir.exists():
            return {}
        out: dict[str, tuple[str, str]] = {}
        for pattern in ("**/*.md", "**/*.txt", "**/*.pdf"):
            for p in data_dir.glob(pattern):
                if p.is_file():
                    try:
                        out[_to_rel(p)] = (str(p), file_sha256(p))
                    except OSError as e:
                        print(f"[KnowledgeBase] 读取失败 {p.name}：{e}")
        return out

    def _indexed_files(self) -> dict[str, tuple[str, str | None]]:
        """从向量库 metadata 读回 {相对标识: (原始 source, file_hash)}。

        同时保留**原始 source**：删除操作（`where={"source": ...}`）必须使用索引中
        记录的那个字符串，而比较用的是相对标识。
        """
        out: dict[str, tuple[str, str | None]] = {}
        try:
            got = self._vector_store.get(include=["metadatas"])
        except Exception as e:
            print(f"[KnowledgeBase] 读取索引元数据失败：{e}")
            return out
        for md in got.get("metadatas") or []:
            src = (md or {}).get("source")
            if src:
                out[_to_rel(src)] = (str(src), (md or {}).get("file_hash"))
        return out

    def diff_index(self) -> dict[str, list[str]]:
        """对比「data/ 现状」与「向量库已索引」，返回差异（**只检测、不修改**）。

        供 CLI 的 status 命令与应用启动时的同步规划使用。

        Returns:
            {"added": [...], "updated": [...], "removed": [...]}
            · added / updated：**相对标识**（如 `新疆_栽培与水肥/x.md`）
            · removed：索引中的**原始 source**（可直接用作删除的 where 条件）
            updated 由**内容 hash** 变化判定，而非文件修改时间（mtime 不可靠）。
        """
        files = self._scan_files()                    # {相对标识: (绝对路径, hash)}
        indexed = self._indexed_files()               # {相对标识: (原始 source, hash)}
        return {
            "added": sorted(r for r in files if r not in indexed),
            "updated": sorted(r for r in files
                              if r in indexed and indexed[r][1] != files[r][1]),
            "removed": sorted(indexed[r][0] for r in indexed if r not in files),
        }

    def _load_and_split_files(self, rels: list[str]) -> list:
        """只对给定文件（**相对标识**）做加载与切分，并写入 source / file_hash 元数据。"""
        documents = []
        for rel in rels:
            path = _abs_path(rel)                    # 相对标识 → 绝对路径
            try:
                if path.suffix.lower() == ".pdf":
                    loaded = PyMuPDFLoader(str(path)).load()
                else:
                    loaded = TextLoader(str(path), encoding="utf-8").load()
            except Exception as e:
                print(f"[KnowledgeBase] 加载失败 {path.name}：{e}")
                continue
            digest = file_sha256(path)
            source = _to_source(rel)                 # 稳定相对标识：data/<相对路径>
            for d in loaded:
                d.metadata["source"] = source
                d.metadata["file_hash"] = digest
            documents.extend(loaded)

        if not documents:
            return []
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            separators=["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", " ", ""],
        )
        return splitter.split_documents(documents)

    def remove_documents(self, sources: list[str]) -> int:
        """按来源文件移除其全部片段，返回移除的片段数。

        Args:
            sources: 来源路径列表，需与 metadata["source"] 完全一致。
        """
        if not sources:
            return 0
        try:
            got = self._vector_store.get(
                where={"source": {"$in": sources}}, include=["metadatas"],
            )
            ids = got.get("ids") or []
            if ids:
                self._vector_store.delete(ids=ids)
            return len(ids)
        except Exception as e:
            print(f"[KnowledgeBase] 移除片段失败：{e}")
            return 0

    def sync_index(self, dry_run: bool = False) -> dict[str, int]:
        """增量同步：只处理发生变化的文件，**不重建整个向量库**。

        · 新增文件 → 切分 + 向量化 + 入库
        · 内容变化 → 先删旧片段，再重新切分入库（改 = 删 + 增）
        · 文件被删 → 按 source 移除其全部片段

        Args:
            dry_run: 仅统计差异，不写入向量库。

        Returns:
            {"added","updated","removed","chunks_added","chunks_removed"}
        """
        diff = self.diff_index()
        added, updated, removed = diff["added"], diff["updated"], diff["removed"]
        result = {"added": len(added), "updated": len(updated), "removed": len(removed),
                  "chunks_added": 0, "chunks_removed": 0}
        if dry_run or not (added or updated or removed):
            return result

        # ① 先删：已删除的文件 + 内容变化的文件（后者稍后重新入库）
        #    删除必须使用索引中记录的**原始 source** 字符串（updated 里是文件路径）
        indexed = self._indexed_files()
        stale = list(removed)
        for norm in updated:
            entry = indexed.get(norm)
            if entry:
                stale.append(entry[0])
        if stale:
            result["chunks_removed"] = self.remove_documents(stale)

        # ② 再增：新增文件 + 内容变化的文件
        to_index = added + updated
        if to_index:
            chunks = self._load_and_split_files(to_index)
            if chunks:
                self._vector_store.add_documents(chunks)
                result["chunks_added"] = len(chunks)
        return result

    # ---- Rerank 精排 ----

    def _rerank_docs(self, query: str, docs: list, top_n: int) -> list:
        """用 CrossEncoder（硅基流动 rerank API）对候选文档精排。

        向量检索侧重整体语义，对"灌溉 vs 施肥"这类**词面细微差异**不敏感；
        Rerank 让模型逐条比对 (query, 文档) 的相关性，可显著改善排序。

        Args:
            query: 检索查询。
            docs: 候选 Document 列表（向量召回结果，未精排）。
            top_n: 精排后保留条数。

        Returns:
            精排后的 Document 列表；**任何异常都降级为原顺序前 top_n 条**
            ——精排是增益项，不应让主链路失败。
        """
        if not docs or top_n <= 0:
            return docs
        if len(docs) == 1:
            return docs[:top_n]
        try:
            resp = requests.post(
                AppConfig.EMBEDDING_BASE_URL.rstrip("/") + "/rerank",
                headers={"Authorization": "Bearer " + AppConfig.EMBEDDING_API_KEY},
                json={
                    "model": AppConfig.RERANK_MODEL,
                    "query": query,
                    # 防御：单条文档截断，避免超长文本触发 API 参数错误
                    "documents": [d.page_content[:2000] for d in docs],
                    "top_n": min(top_n, len(docs)),
                    "return_documents": False,
                },
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
            ordered = [docs[r["index"]] for r in results
                       if isinstance(r.get("index"), int) and 0 <= r["index"] < len(docs)]
            if ordered:
                return ordered[:top_n]
            print("[Rerank] 返回为空，降级为向量召回顺序。")
        except Exception as e:
            print(f"[Rerank] 精排失败（降级为向量召回顺序）: {e}")
        return docs[:top_n]

    # ---- 多路排序融合（RRF）----

    def _fuse_rrf(self, pool: list, reranked: list, top_n: int, k: int = 60) -> list:
        """用 RRF（倒数排名融合）合并"向量顺序"与"Rerank 顺序"。

        RRF 只使用**名次**、不使用分数——向量给的是距离、Rerank 给的是
        0~1 相关性分，两者量纲不可比；而名次天然可比：

            score(d) = 1/(k + rank_vector(d)) + 1/(k + rank_rerank(d))

        设计意图：向量路擅长**语义等价**（同义词/俗名，如"红蜘蛛"＝"棉叶螨"），
        Rerank 路擅长**细粒度排序**；融合让"两路都不差"的文档胜出，
        以弥补 Rerank 在同义词上的字面盲区。

        注意：本方法两路的候选集相同（Rerank 的输入即向量召回结果），
        因此属"排序折中"而非教科书式的独立多路召回融合。

        Args:
            pool: 向量召回结果（原始顺序）。
            reranked: 同一批候选经 Rerank 后的顺序。
            top_n: 融合后保留条数。
            k: RRF 平滑常数（经验值 60，用于压低高名次间的边际差异）。

        Returns:
            融合排序后的 Document 列表（前 top_n 条）。
        """
        if not pool:
            return []
        if len(pool) == 1 or not reranked:
            return pool[:top_n]

        vec_rank = {id(d): i for i, d in enumerate(pool, 1)}
        rr_rank = {id(d): i for i, d in enumerate(reranked, 1)}

        scored = []
        for d in pool:
            key = id(d)
            s = 0.0
            if key in vec_rank:
                s += 1.0 / (k + vec_rank[key])
            if key in rr_rank:
                s += 1.0 / (k + rr_rank[key])
            scored.append((s, d))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [d for _, d in scored[:top_n]]

    def retrieve_context(self, query: str, k: int = 3) -> str:
        """语义检索（+ 可选 Rerank 精排）后返回 Top-K 文档片段。

        `RERANK_ENABLED` 开启时：先向量召回 `RERANK_RECALL_K` 条候选，
        再精排取 top-k；关闭时退化为纯向量检索 top-k。

        Args:
            query: 查询文本。
            k: 最终返回的文档块数量，默认 3。

        Returns:
            拼接后的上下文字符串，各片段以双换行分隔；
            若无结果则返回空字符串。
        """
        if AppConfig.RERANK_ENABLED and AppConfig.RERANK_RECALL_K > k:
            pool = self._vector_store.similarity_search(query, k=AppConfig.RERANK_RECALL_K)
            docs = self._rerank_docs(query, pool, top_n=k)
        else:
            docs = self._vector_store.similarity_search(query, k=k)
        if not docs:
            print("[KnowledgeBase] 未检索到相关文档。")
            return ""
        contexts = [doc.page_content for doc in docs]
        return "\n\n".join(contexts)
