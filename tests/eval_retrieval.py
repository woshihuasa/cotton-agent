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
  2. 对每条 query，直接查询向量库（kb._vector_store.similarity_search，取 top-3），
     拿到命中文档的**来源文件名**（Document.metadata["source"]）
  3. 计算指标：
       - Hit@1 / Hit@3：期望文档是否出现在第 1 / 前 3 位
       - MRR（Mean Reciprocal Rank）：首个命中位置的倒数均值
         （第 1 位命中记 1.0，第 2 位记 0.5，第 3 位记 0.33，未命中记 0）
  4. 输出逐条结果 + 三项指标 + 未命中明细

【指标定义与目标】
  - Hit@3 = 期望文档出现在 top-3 的样本比例（目标 ≥ 85%）
  - MRR   = Σ(1/首个命中排名) / 样本数（目标 ≥ 0.7）
  - 命中判定：expected_docs 为列表时，命中任一即算命中（同主题多篇年份文档）

【为什么直连向量库而不是用 retrieve_context()】
  retrieve_context() 只返回拼接后的文本，丢失了文档来源信息；
  评测需要"命中了哪篇文档"才能判定对错，因此直接调用底层 similarity_search 取 Document
  （含 metadata）。这正是"评测要能观测到判定依据"的设计要求。

【用法】
  .venv\\Scripts\\python.exe tests\\eval_retrieval.py            # 跑全部 27 条
  .venv\\Scripts\\python.exe tests\\eval_retrieval.py --k 5      # 用 top-5 检索（看更大召回的潜力）
  .venv\\Scripts\\python.exe tests\\eval_retrieval.py --verbose  # 打印每条命中的文档列表

【成本】
  每条 1 次 Embedding API 调用（query 向量化），27 条约几分钱；无 LLM 生成开销。

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
    parser.add_argument("--k", type=int, default=3, help="检索条数（默认 3，与生产一致）")
    parser.add_argument("--verbose", action="store_true", help="打印每条命中的文档列表")
    args = parser.parse_args()

    cases = load_cases(os.path.join("tests", "eval_data", "retrieval_set.jsonl"))
    kb = KnowledgeBase()
    print("检索质量评测：%d 条样本，top-k = %d\n" % (len(cases), args.k))

    hit1 = hitk = 0
    rr_sum = 0.0
    miss = []

    for case in cases:
        query = case["query"]
        expected = case["expected_docs"]

        docs = kb._vector_store.similarity_search(query, k=args.k)
        hits = [doc_name(d) for d in docs]

        rank = first_hit_rank(hits, expected)
        hit1 += (rank == 1)
        hitk += (rank > 0)
        rr_sum += (1.0 / rank) if rank else 0.0

        if rank == 0:
            miss.append((case["id"], query, expected, hits))

        if args.verbose:
            print("  #%2d %s | 排名=%s" % (case["id"], query, rank or "未命中"))
            print("        期望: %s" % expected)
            print("        实际: %s" % hits)

    total = len(cases)
    print("\n" + "=" * 60)
    print("Hit@1: %d/%d = %.1f%%" % (hit1, total, hit1 / total * 100))
    print("Hit@%d: %d/%d = %.1f%%  （目标 ≥ 85%%）" % (args.k, hitk, total, hitk / total * 100))
    print("MRR  : %.3f  （目标 ≥ 0.7）" % (rr_sum / total))
    if miss:
        print("\n未命中明细（%d 条）:" % len(miss))
        for cid, query, expected, hits in miss:
            print("  #%2d %s" % (cid, query))
            print("        期望: %s" % expected)
            print("        实际: %s" % hits)
    else:
        print("全部命中 ✓")


if __name__ == "__main__":
    main()
