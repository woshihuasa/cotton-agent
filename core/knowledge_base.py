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
import re
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


# ---------------------------------------------------------------------------
# 时效性（2026-10-18 新增）
# ---------------------------------------------------------------------------
#
# 【为什么需要】知识库里同一主题有多个年份版本 —— 实测：
#
#     棉花科学灌溉技术指导意见  → 2023 / 2024 / 2025 / 2026（4 个版本）
#     棉花春季生产技术指导意见  → 2025 / 2026 / 无年份
#     棉花科学施肥指导意见      → 2025 / 2026
#
# 而这些**年度指导意见的内容逐年高度重复**（2023 与 2025 的花铃期段落**逐字相同**），
# 于是向量检索会**同时命中多个年份**，且**无法区分**。
#
# 更糟的是原来的 `retrieve_context()` 只返回正文、**把 source 整个丢掉** ——
# 模型根本看不到自己引用的是哪一年的文件，**时效性就此消失**。
#
# 所以这里做三件事：① 解析年份；② 写进 metadata；③ 检索时带回来源并优先最新。

_YEAR_RE = re.compile(r"(20\d\d)\s*年")


def doc_year(source: str | Path) -> int | None:
    """从来源标识里解析**文档年份**（如 `2026年棉花科学灌溉技术指导意见.md` → 2026）。

    ★ 只看**文件名**，不看正文 —— 因为文件名是编制者显式标出的"这是哪一年的指导意见"，
      而正文里出现的年份可能是引用别的年份的数据（会误判）。
    ★ 解析不到就返回 `None`（**不猜**）—— 例如「棉花应对高温天气生产技术指导意见」本身就不带年份。
    """
    name = Path(str(source).replace("\\", "/")).name
    m = _YEAR_RE.search(name)
    return int(m.group(1)) if m else None


def chunk_year(metadata: dict) -> int | None:
    """取一个切片的年份：优先用 metadata 里显式存的，没有就从 `source` 现解析。

    ★ 为什么要这个"回退"：改造前入库的切片**没有 `year` 字段**（实测 152 片全是旧的）。
      有了回退，**不必重建索引**就能让老切片也有年份 —— 否则必须全量重嵌（要花 Embedding 费用）。
    """
    y = (metadata or {}).get("year")
    if isinstance(y, int):
        return y
    try:
        return doc_year((metadata or {}).get("source", ""))
    except (TypeError, ValueError):
        return None


def _topic_key(source: str | Path) -> tuple[str, str]:
    """同主题分组的键：`(所在目录, 去掉年份的主题名)`。

    例：`data/新疆_栽培与水肥/2026年棉花科学灌溉技术指导意见.md`
      → (`新疆_栽培与水肥`, `棉花科学灌溉技术指导意见`)
    """
    s = str(source).replace("\\", "/")
    p = Path(s)
    parent = p.parent.name if p.parent.name not in ("data", "") else ""
    topic = _YEAR_RE.sub("", p.stem).strip() or p.stem
    return (parent, topic)


def _dedupe_by_topic(docs: list) -> list:
    """**同主题只保留最新年份那一版**，其余顺序不变。

    【要解决的问题】同主题多版本同时被检索到，有两个坏后果：
      ① 模型不知道该信哪一年；
      ② 多版本**挤占 top-k 名额**（比如 k=3 时 3 个名额全被"科学灌溉"的不同年份占满，
         其他有用内容一个都进不来）。

    【规则】
      · 按 `_topic_key` 分组；**组内取年份最大者**；
      · **没有年份的切片不参与比较** —— 它们各自独立成组（不猜年份，也不淘汰别人）。
        例如「棉花应对高温天气生产技术指导意见」本身不带年份，应保留；
      · 保留"该组最佳那一条在原文里的位置"，让最终排序仍按相关度。

    ★ 为什么用"分组取最大"而不是"直接按年份排序"：
      排序会让所有 2026 年的文档一拥而上，**把相关度更高的旧文档压到后面**。
      分组取最大只解决"同主题重复"，**不动跨主题的相关度顺序**。
    """
    best: dict[tuple[str, str], tuple[int, int]] = {}   # key → (year, index)
    for i, d in enumerate(docs or []):
        md = getattr(d, "metadata", {}) or {}
        y = chunk_year(md)
        if y is None:
            continue                                     # 无年份 → 不参与去重
        key = _topic_key(md.get("source", ""))
        cur = best.get(key)
        if cur is None or y > cur[0]:
            best[key] = (y, i)

    keep = {idx for _, idx in best.values()}
    out = []
    for i, d in enumerate(docs or []):
        y = chunk_year(getattr(d, "metadata", {}) or {})
        if y is None or i in keep:
            out.append(d)
    return out


def _format_chunk_with_source(doc) -> str:
    """把一段切片格式化成**模型看得见出处与年份**的文本。

    形如：

        〔出处：2026年棉花科学灌溉技术指导意见.md ｜ 年份：2026〕
        ### 各生育期灌溉方案
        ...

    ★ 为什么把年份单独再写一遍（文件名里已经有了）：**文件名不一定被模型注意到**，
      单拎出来放一个明确的"年份"字段，模型引用时更容易带上它。
    """
    md = getattr(doc, "metadata", {}) or {}
    src = str(md.get("source", "")).replace("\\", "/")
    name = Path(src).name if src else "（未知来源）"
    y = chunk_year(md)
    year_txt = str(y) if y is not None else "未标注"
    head = f"〔出处：{name} ｜ 年份：{year_txt}〕"
    return head + "\n" + (doc.page_content or "").strip()


def _abs_path(rel: str | Path) -> Path:
    """由相对标识还原为绝对路径（用于加载文件与计算指纹）。"""
    return Path(AppConfig.DATA_DIR) / _to_rel(rel)


def _annotate_chunk(doc, *, rel: str, digest: str) -> None:
    """给一个切片写入统一元数据 —— **全量重建与增量入库共用这一份**。

    ★ 为什么要抽成一个函数：写入路径有两条（`_load_and_split_documents` 全量、
      `_load_and_split_files` 增量）。两处各写一遍就会漂移 ——
      这正是本项目反复强调的「**两份地图必然对不上**」。
    """
    doc.metadata["source"] = _to_source(rel)
    doc.metadata["file_hash"] = digest
    y = doc_year(rel)
    if y is not None:
        doc.metadata["year"] = y


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
            if rel not in hash_cache:
                try:
                    hash_cache[rel] = file_sha256(_abs_path(rel))
                except OSError:
                    hash_cache[rel] = ""
            _annotate_chunk(c, rel=rel, digest=hash_cache[rel])
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

        ★★ 2026-10-18 加固：**同一个文件在索引里可能存在多种 `source` 写法**。
          实测过：152 个切片里有 128 个存的是绝对路径 `E:\\...\\x.md`、24 个存的是
          相对标识 `data/x.md`（相对标识是后来改造引入的，旧切片没重嵌）。

          而本函数是 `out[归一化] = (原始 source, hash)` —— **后写覆盖先写**。
          若某文件同时有两种写法，只会记住最后读到的那一种，
          `remove_documents()` 就**只删得掉一半片段，另一半永远残留**。

          所以这里**把同一文件的所有原始写法都记下来**，删除时**逐个删**。
          不靠"希望索引里只有一种格式"来保证正确性。
        """
        out: dict[str, tuple[str, str | None]] = {}
        aliases: dict[str, set[str]] = {}
        try:
            got = self._vector_store.get(include=["metadatas"])
        except Exception as e:
            print(f"[KnowledgeBase] 读取索引元数据失败：{e}")
            return out
        for md in got.get("metadatas") or []:
            src = (md or {}).get("source")
            if not src:
                continue
            key = _to_rel(src)
            out[key] = (str(src), (md or {}).get("file_hash"))
            aliases.setdefault(key, set()).add(str(src))
        # 把"同一文件的其它写法"挂到实例上，供 remove_documents 使用
        self._source_aliases = aliases
        return out

    def source_variants(self, rel: str) -> list[str]:
        """给定相对标识，返回**索引里它出现过的全部原始 source 写法**。

        用于删除时**逐个尝试**，避免"只能删掉一种格式"导致的残留。
        若还没调用过 `_indexed_files()`（缓存为空），返回只含相对标识自身的列表。
        """
        aliases = getattr(self, "_source_aliases", None) or {}
        variants = sorted(aliases.get(rel) or [])
        if not variants:
            variants = [_to_source(rel)]
        elif _to_source(rel) not in variants:
            variants.append(_to_source(rel))
        return variants

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
            for d in loaded:
                _annotate_chunk(d, rel=rel, digest=digest)
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
        #    ★ 2026-10-18 加固：同一文件在索引里可能有**多种 source 写法**
        #      （绝对路径 / 相对标识共存）。所以这里对每个文件**展开成全部写法**，
        #      否则只删得掉一种格式的片段，另一种**永远残留**。
        indexed = self._indexed_files()
        stale: list[str] = []
        seen: set[str] = set()

        def _expand(src_or_rel: str, *, is_rel: bool = False) -> None:
            """把一个来源展开成它在索引里出现过的**全部写法**，去重后加入待删列表。"""
            variants = (self.source_variants(src_or_rel) if is_rel else [src_or_rel])
            for v in variants:
                if v not in seen:
                    seen.add(v)
                    stale.append(v)

        for src in removed:                       # removed 里已是原始 source
            _expand(src)
        for rel in updated:                       # updated 里是相对标识
            entry = indexed.get(rel)
            if entry:
                _expand(rel, is_rel=True)
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

    def retrieve_context(
        self, query: str, k: int = 3, *,
        with_source: bool = True, prefer_latest: bool = True,
    ) -> str:
        """语义检索（+ 可选 Rerank 精排）后返回 Top-K 文档片段。

        `RERANK_ENABLED` 开启时：先向量召回 `RERANK_RECALL_K` 条候选，
        再精排取 top-k；关闭时退化为纯向量检索 top-k。

        Args:
            query: 查询文本。
            k: 最终返回的文档块数量，默认 3。
            with_source: ★ 每段前面**带上出处与年份**（默认开）。
                关掉就退回改造前的行为（只拼正文）—— 保留这个开关是为了让
                "老行为"可复现，便于对照。
            prefer_latest: ★ **同主题只保留最新年份那一版**（默认开）。
                同主题的判定见 `_topic_key()`。

        Returns:
            拼接后的上下文字符串，各片段以双换行分隔；
            若无结果则返回空字符串。

        【为什么有这两个开关（2026-10-18）】
            知识库里同一主题存了多个年份版本，且**年度指导意见内容逐年高度重复**
            （2023 与 2025 的花铃期段落**逐字相同**）。后果有两个：
              ① 检索会**同时命中多个年份**，模型无法判断该信哪个；
              ② 同主题的多个版本**挤占 top-k 名额**，把其他有用内容挤出去。
            加上原来的实现**只返回正文、丢掉 source**，模型连"这是哪一年的"都看不到。
        """
        # ① 召回：`prefer_latest` 时多取一些候选，给"按主题去重"留出空间
        want = max(k, 1)
        pool_k = want * 4 if prefer_latest else want
        if AppConfig.RERANK_ENABLED and AppConfig.RERANK_RECALL_K > want:
            pool = self._vector_store.similarity_search(
                query, k=max(AppConfig.RERANK_RECALL_K, pool_k))
        else:
            pool = self._vector_store.similarity_search(query, k=pool_k)

        if not pool:
            print("[KnowledgeBase] 未检索到相关文档。")
            return ""

        # ② 精排（仅在候选多于 k 且开了 rerank 时才有意义）
        if AppConfig.RERANK_ENABLED and AppConfig.RERANK_RECALL_K > want:
            docs = self._rerank_docs(query, pool, top_n=pool_k)
        else:
            docs = pool

        # ③ 同主题优先最新
        if prefer_latest:
            docs = _dedupe_by_topic(docs)

        docs = docs[:want]
        if not docs:
            print("[KnowledgeBase] 未检索到相关文档。")
            return ""

        # ④ 拼接：带上出处与年份
        if with_source:
            return "\n\n".join(_format_chunk_with_source(d) for d in docs)
        return "\n\n".join(doc.page_content for doc in docs)
