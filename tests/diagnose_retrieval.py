# -*- coding: utf-8 -*-
"""
检索诊断（Retrieval Diagnosis）
================================

【用途】
  针对**单条样本**，把"机器视角"完整暴露出来，定位"为什么这条没进 top-3"。
  评测脚本只告诉你"排名第几"，本脚本告诉你"模型当时看到的是哪段文字"。

【输出四块信息】
  1. 向量召回顺序（top-N）           —— 基准排序
  2. Rerank 精排后的顺序（top-N）    —— 精排做了什么改动
  3. 期望文档被召回的 chunk **全文**  —— 关键：它是否真的包含答案要素？
  4. Rerank 后胜出者的 chunk 全文     —— 关键：它凭什么排上来？

【它能回答的两个典型根因】
  · **chunk 粒度问题**：期望 chunk 里没有答案关键词（答案被切到了相邻 chunk）
    → 说明模型看到的是"残缺片段"，应上父子索引（small-to-big）
  · **语义偏好问题**：期望 chunk 含全部答案要素，但仍被排在后面
    → 说明 reranker/向量模型判断与标注口径不一致，属模型能力或标注边界问题

【用法】
  .venv\\Scripts\\python.exe tests\\diagnose_retrieval.py --id 21
  .venv\\Scripts\\python.exe tests\\diagnose_retrieval.py --id 3 --keywords "缩节胺,化控,喷施"
  .venv\\Scripts\\python.exe tests\\diagnose_retrieval.py --query "棉田红蜘蛛什么时候全田防治" --doc "病虫害"

【成本】
  每条 1 次 Embedding + 1 次 Rerank API 调用（均为免费档模型）。
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

DATASET = os.path.join("tests", "eval_data", "retrieval_set.jsonl")
PREVIEW = 700          # 片段正文打印长度上限


def doc_name(doc) -> str:
    try:
        return Path(doc.metadata.get("source", "") or "").name
    except AttributeError:
        return ""


def load_case(case_id: int) -> dict:
    with open(DATASET, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                c = json.loads(line)
                if c["id"] == case_id:
                    return c
    raise SystemExit("评测集中没有 id=%s 的样本" % case_id)


def show(text: str, limit: int = PREVIEW) -> str:
    text = text.replace("\r", "")
    return text if len(text) <= limit else text[:limit] + "\n  …（截断，共 %d 字）" % len(text)


def main() -> None:
    ap = argparse.ArgumentParser(description="单条检索样本诊断")
    ap.add_argument("--id", type=int, help="评测集样本 id（推荐）")
    ap.add_argument("--query", type=str, help="自定义 query（与 --doc 搭配）")
    ap.add_argument("--doc", type=str, default="", help="期望文档名包含的子串（自定义模式用）")
    ap.add_argument("--keywords", type=str, default="",
                    help="答案关键词，逗号分隔；用于检查期望片段是否含答案要素")
    ap.add_argument("--k", type=int, default=20, help="召回/精排条数（默认 20）")
    args = ap.parse_args()

    if args.id is not None:
        case = load_case(args.id)
        query, expected = case["query"], list(case["expected_docs"])
    elif args.query:
        query, expected = args.query, ([args.doc] if args.doc else [])
    else:
        raise SystemExit("请用 --id 或 --query 指定诊断目标")

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    kb = KnowledgeBase()
    print("=" * 70)
    print("Query   :", query)
    print("期望文档:", expected or "（未指定）")
    if keywords:
        print("答案关键词:", keywords)
    print("=" * 70)

    pool = kb._vector_store.similarity_search(query, k=args.k)
    reranked = kb._rerank_docs(query, pool, top_n=args.k)

    # ---- 1. 向量召回顺序 ----
    print("\n【1】向量召回顺序（top-8）")
    vec_rank = {id(d): i for i, d in enumerate(pool, 1)}
    for i, d in enumerate(pool[:8], 1):
        mark = "  ← 期望" if doc_name(d) in expected else ""
        print("  %2d. %s%s" % (i, doc_name(d), mark))

    # ---- 2. Rerank 顺序 ----
    print("\n【2】Rerank 精排后顺序（top-8）")
    for i, d in enumerate(reranked[:8], 1):
        mark = "  ← 期望" if doc_name(d) in expected else ""
        print("  %2d. %s  （向量原排名 %d）%s" % (i, doc_name(d), vec_rank.get(id(d), 0), mark))

    # ---- 3. 期望文档的召回片段全文 ----
    print("\n【3】期望文档被召回的片段（全文）——判断是否含答案要素")
    exp_hits = [(i, d) for i, d in enumerate(pool, 1) if doc_name(d) in expected]
    if not exp_hits:
        print("  ⚠️ 期望文档**一条 chunk 都没召回**（属召回问题，而非排序问题）")
    for i, d in exp_hits:
        print("\n  ── 向量排名 %d ｜ %s ──" % (i, doc_name(d)))
        if keywords:
            flags = "  ".join("%s=%s" % (k, "✓" if k in d.page_content else "✗") for k in keywords)
            print("  关键词命中: " + flags)
        print("  " + show(d.page_content).replace("\n", "\n  "))

    # ---- 4. Rerank 胜出者的片段全文 ----
    print("\n【4】Rerank 后 top-3 的片段（全文）——它们凭什么排上来")
    for i, d in enumerate(reranked[:3], 1):
        print("\n  ── 第 %d 位 ｜ %s ｜ 向量原排名 %d ──"
              % (i, doc_name(d), vec_rank.get(id(d), 0)))
        print("  " + show(d.page_content).replace("\n", "\n  "))

    print("\n" + "=" * 70)
    print("判读要点：")
    print("  · 若【3】的关键词命中含 ✗ → **chunk 粒度问题**（答案被切到相邻块），应考虑父子索引")
    print("  · 若【3】关键词全 ✓ 但仍排后 → **语义/标注边界问题**，看【4】胜出片段是否确实部分相关")
    print("  · 若【3】显示连一条 chunk 都没召回 → **召回问题**，需混合检索而非 Rerank")


if __name__ == "__main__":
    main()
