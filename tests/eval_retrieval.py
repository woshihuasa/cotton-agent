# -*- coding: utf-8 -*-
"""
检索质量评测（Retrieval Quality Evaluation）
=============================================

【脚本功能】
  评测 RAG 检索层的召回质量 —— 用户问题能否检索到**包含答案的文档**。
  这是"检索与生成分开评"原则中的**检索侧**：只评检索，不涉及 LLM 生成，
  因此结果不受模型幻觉/表述影响，能精确定位问题（检索错了 vs 生成错了）。

【评测流程】
  1. 读取评测集 tests/eval_data/retrieval_set.jsonl（27 条：query → expected_docs）
  2. 对每条 query 直接查询向量库（kb._vector_store.similarity_search），
     取 **探针 top-K（默认 20）**，拿到命中文档的来源文件名（Document.metadata["source"]）
  3. 计算指标：
       - Hit@K：期望文档是否出现在前 K 位（报 1 / 生产 k / 10 / 探针 k 四档）
       - MRR（Mean Reciprocal Rank）：首个命中位置的倒数均值
  4. **未命中归因**（本脚本的核心增值）：
       - 排序问题：答案在探针 top-K 内，但没进生产 top-k  → Rerank/精排可解
       - 召回问题：答案连探针 top-K 都进不去            → 需混合检索（BM25）提升召回
     这一区分直接决定优化优先级，避免"盲上 Rerank"（若答案压根不在候选池里，
     精排无从谈起）。

【指标定义与目标】
  - Hit@3 = 期望文档出现在 top-3 的样本比例（目标 ≥ 85%）
  - MRR   = Σ(1/首个命中排名) / 样本数（目标 ≥ 0.7）
  - 命中判定：expected_docs 为列表时，命中任一即算命中（同主题多篇年份文档）

【为什么直连向量库而不是用 retrieve_context()】
  retrieve_context() 只返回拼接后的文本，丢失了文档来源信息；
  评测需要"命中了哪篇文档"才能判定对错，因此直接调用底层 similarity_search 取 Document
  （含 metadata）。这正是"评测要能观测到判定依据"的设计要求。

【用法】
  .venv\\Scripts\\python.exe tests\\eval_retrieval.py               # 全部 27 条（探针 top-20）
  .venv\\Scripts\\python.exe tests\\eval_retrieval.py --k 5         # 生产配置改为 top-5
  .venv\\Scripts\\python.exe tests\\eval_retrieval.py --verbose     # 打印每条命中的文档列表
  .venv\\Scripts\\python.exe tests\\eval_retrieval.py --probe-k 30  # 放大探针范围

【成本】
  每条 1 次 Embedding API 调用（query 向量化），27 条约几分钱；无 LLM 生成开销。
  探针取 top-K 不增加 API 调用次数（只影响本地返回条数）。

【后续用途】
  引入混合检索（BM25 + 向量 + RRF）与 Rerank 后，用本脚本对比 Hit@3 / MRR 的提升幅度，
  作为"优化有效"的量化证据（当前基线见 eval_report.md）。
"""
import io
import json
import os
import sys
import argparse
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.knowledge_base import KnowledgeBase      # noqa: E402


def load_cases(path: str) -> list[dict]:
    """读取检索评测集（JSONL）。"""
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def doc_name(doc) -> str:
    """从 Document metadata 提取来源文件名（用于与 expected_docs 比对）。"""
    src = ""
    try:
        src = doc.metadata.get("source", "") or ""
    except AttributeError:
        src = ""
    return Path(src).name if src else ""


def first_hit_rank(hits: list[str], expected: list[str]) -> int:
    """返回首个命中的排名（1-based）；未命中返回 0。"""
    for i, name in enumerate(hits, 1):
        if name in expected:
            return i
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="检索质量评测（Hit@K / MRR）")
    parser.add_argument("--k", type=int, default=3, help="生产使用的检索条数（默认 3，与生产一致）")
    parser.add_argument("--probe-k", type=int, default=20,
                        help="探针检索条数（默认 20；用于 Hit@10/20 与归因，不影响生产配置）")
    parser.add_argument("--verbose", action="store_true", help="打印每条命中的文档列表")
    parser.add_argument("--rerank", action="store_true",
                        help="对候选池执行 Rerank 精排后再计算指标（与纯向量结果对比）")
    parser.add_argument("--fuse", action="store_true",
                        help="RRF 融合「向量顺序 + Rerank 顺序」后再计算指标")
    args = parser.parse_args()

    probe_k = max(args.probe_k, args.k, 1)

    cases = load_cases(os.path.join("tests", "eval_data", "retrieval_set.jsonl"))
    kb = KnowledgeBase()
    if args.fuse:
        mode = "RRF 融合（向量顺序 + Rerank 顺序）"
    elif args.rerank:
        mode = "纯向量 + Rerank 精排"
    else:
        mode = "纯向量"
    print("检索质量评测：%d 条样本 | 模式 = %s | 生产 top-k = %d | 探针 top-k = %d\n"
          % (len(cases), mode, args.k, probe_k))

    ranks: list[int] = []
    details: list[tuple] = []

    for case in cases:
        query = case["query"]
        expected = case["expected_docs"]

        docs = kb._vector_store.similarity_search(query, k=probe_k)
        if args.fuse:
            # 两路候选集相同：Rerank 只重排向量召回的这批，再与向量名次做 RRF 融合
            reranked = kb._rerank_docs(query, docs, top_n=probe_k)
            docs = kb._fuse_rrf(docs, reranked, top_n=probe_k)
        elif args.rerank:
            # 精排同样保留 probe_k 条，便于与纯向量顺序逐档对比（Hit@1/3/10/20）
            docs = kb._rerank_docs(query, docs, top_n=probe_k)
        hits = [doc_name(d) for d in docs]

        rank = first_hit_rank(hits, expected)
        ranks.append(rank)
        details.append((case["id"], query, expected, hits, rank))

        if args.verbose:
            print("  #%2d %s | 排名=%s" % (case["id"], query, rank or "未命中"))
            print("        期望: %s" % expected)
            print("        实际: %s" % hits)

    total = len(cases)

    def hit_at(k: int) -> int:
        return sum(1 for r in ranks if 0 < r <= k)

    hit1 = hit_at(1)
    hitk = hit_at(args.k)
    rr_sum = sum((1.0 / r) if r else 0.0 for r in ranks)

    print("\n" + "=" * 60)
    print("Hit@1 : %d/%d = %.1f%%" % (hit1, total, hit1 / total * 100))
    print("Hit@%d: %d/%d = %.1f%%  （生产配置，目标 ≥ 85%%）"
          % (args.k, hitk, total, hitk / total * 100))
    for k in (10, probe_k):
        if k > args.k:
            h = hit_at(k)
            print("Hit@%-2d: %d/%d = %.1f%%  （探针）" % (k, h, total, h / total * 100))
    print("MRR  : %.3f  （目标 ≥ 0.7）" % (rr_sum / total))

    # ---- 归因：排序问题（Rerank 可解） vs 召回问题（需混合检索）----
    sortable = [d for d in details if 0 < d[4] <= probe_k and d[4] > args.k]
    recall_miss = [d for d in details if d[4] == 0]

    print("\n" + "=" * 60)
    print("未命中归因（决定优化优先级）:")
    print("  【排序问题】答案在 top-%d 内但未进 top-%d：%d 条  → 精排/Rerank 可解"
          % (probe_k, args.k, len(sortable)))
    print("  【召回问题】答案不在 top-%d：%d 条             → 需混合检索（BM25）提升召回"
          % (probe_k, len(recall_miss)))

    if sortable:
        print("\n  排序问题明细（这些是 Rerank 的直接收益）:")
        for cid, query, expected, hits, rank in sortable:
            print("    #%2d %s" % (cid, query))
            print("        期望: %s | 当前排名: %d" % (expected, rank))
            print("        top-5 实际: %s" % hits[:5])

    if recall_miss:
        print("\n  召回问题明细（Rerank 无效，需先提升召回）:")
        for cid, query, expected, hits, rank in recall_miss:
            print("    #%2d %s" % (cid, query))
            print("        期望: %s" % expected)
            print("        top-5 实际: %s" % hits[:5])

    if not sortable and not recall_miss:
        print("\n  全部命中 ✓")

    # ---- 结语：给出下一步建议 ----
    print("\n" + "=" * 60)
    if recall_miss:
        print("建议：先做混合检索（BM25 + 向量 + RRF）补召回，再考虑 Rerank。")
    elif sortable:
        print("建议：召回已足够（答案都在候选池内），上 Rerank 精排可直接提升 Hit@%d。" % args.k)
    else:
        print("建议：检索已达目标，可将优化重点转向生成层。")


if __name__ == "__main__":
    main()
