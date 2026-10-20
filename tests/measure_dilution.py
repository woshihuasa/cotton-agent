# -*- coding: utf-8 -*-
"""测量「召回池污染率」—— 统计类大文档对无关问题的稀释程度。

为什么需要它（v0.3.3 知识库治理）：
    27 条检索题的 Hit@3 已达 92.6%，而且其中**只有 2 条**涉及统计文档
    （#26 问品级等级、#27 问统计口径，答案都在「数据说明」段里）。所以把
    23 万字的日度价格表裁掉后，**Hit@K 几乎不可能变化 —— 它测不出这次治理的收益**。

    真正的收益机制是「**召回池被稀释**」：一篇占全库 69% 文字的表格会切出
    502 个低价值 chunk（占全部 760 个片段的 66%），它们会挤占每条 query 的
    top-K 候选席，把真正相关的农艺片段往下压。

    本脚本直接测这个机制：对每条 query 取向量召回 top-K，统计其中来自
    指定「统计类文档」的片段数与占比。

指标读法：
    · **污染率** = top-K 里来自目标文档的片段 / 总召回片段。治理后应显著下降。
    · **受影响 query 数** = 有多少条 query 的候选池里至少混进 1 个目标片段。
      理想情况下农艺问题不该被统计表占据任何席位。

零 LLM：只调用 Embedding（每条 query 一次），无生成模型参与。

用法:
    .venv\\Scripts\\python.exe tests\\measure_dilution.py
    .venv\\Scripts\\python.exe tests\\measure_dilution.py --k 20 --json tests/eval_outputs/dilution_after.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import AppConfig                        # noqa: E402
from core.knowledge_base import KnowledgeBase      # noqa: E402

# 默认的「统计类大文档」——这些是结构化数据，本该由 cotton_price / cotton_stats 工具回答
DEFAULT_TARGETS = (
    "新疆棉花价格指数月度统计.md",
    "新疆棉花产量年度统计.md",
    "新疆棉花播种面积年度统计.md",
)


def doc_name(doc) -> str:
    """从 chunk metadata 取来源文件名（跨平台统一分隔符）。"""
    src = str((doc.metadata or {}).get("source", ""))
    return src.replace("\\", "/").rsplit("/", 1)[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="召回池污染率测量（零 LLM）")
    ap.add_argument("--k", type=int, default=20, help="向量召回条数（默认 20）")
    ap.add_argument("--target", action="append", default=None,
                    help="目标文档文件名，可重复；默认三篇统计文档")
    ap.add_argument("--json", default=None, help="把结果写入 JSON（便于前后对比）")
    ap.add_argument("--quiet", action="store_true", help="不逐条打印")
    args = ap.parse_args()

    targets = tuple(args.target) if args.target else DEFAULT_TARGETS
    cases = [json.loads(line) for line in
             (ROOT / "tests" / "eval_data" / "retrieval_set.jsonl").read_text("utf-8").splitlines()
             if line.strip()]

    kb = KnowledgeBase()

    # 全库片段构成（前后可比的关键读数）
    got = kb._vector_store.get(include=["metadatas"])
    metas = got.get("metadatas") or []
    per_doc = Counter(doc_name(type("D", (), {"metadata": m})()) for m in metas)
    total_chunks = len(metas)
    target_chunks = sum(per_doc.get(t, 0) for t in targets)

    print("=" * 68)
    print(f"召回池污染率测量　|　query {len(cases)} 条　|　top-K = {args.k}")
    print("=" * 68)
    print(f"向量库总片段: {total_chunks}")
    print(f"目标统计文档片段: {target_chunks}  (占全库 {target_chunks / max(total_chunks, 1) * 100:.1f}%)")
    for t in targets:
        print(f"    {per_doc.get(t, 0):>5} 个片段   {t}")
    print(f"其余农艺文档片段: {total_chunks - target_chunks}")
    print("-" * 68)

    total_recalled = 0
    total_polluted = 0
    affected = 0
    worst = (0, None, 0)
    rows = []

    for case in cases:
        query = case["query"]
        docs = kb._vector_store.similarity_search(query, k=args.k)
        names = [doc_name(d) for d in docs]
        n_target = sum(1 for n in names if n in targets)
        total_recalled += len(names)
        total_polluted += n_target
        if n_target:
            affected += 1
        if n_target > worst[0]:
            worst = (n_target, case["id"], query)
        rows.append({"id": case["id"], "query": query, "k": len(names),
                     "from_targets": n_target, "names": names})
        if not args.quiet:
            flag = f"  ⚠ 污染 {n_target}/{len(names)}" if n_target else ""
            print(f"  #{case['id']:>2}  污染 {n_target:>2}/{len(names):<2}{flag}   {query[:38]}")

    rate = total_polluted / max(total_recalled, 1) * 100
    print("=" * 68)
    print("结果")
    print("=" * 68)
    print(f"  召回片段总数        : {total_recalled}")
    print(f"  其中来自统计文档    : {total_polluted}")
    print(f"  >>> 污染率          : {rate:.1f}%")
    print(f"  候选池被混入的 query: {affected}/{len(cases)}")
    if worst[1]:
        print(f"  污染最重的一条      : #{worst[1]}（{worst[0]}/{args.k}）{worst[2][:34]}")
    else:
        print("  污染最重的一条      : 无 —— 统计文档未进入任何候选池")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "k": args.k, "total_chunks": total_chunks, "target_chunks": target_chunks,
            "total_recalled": total_recalled, "polluted": total_polluted,
            "pollution_rate_pct": round(rate, 2), "affected_queries": affected,
            "per_doc_chunks": {t: per_doc.get(t, 0) for t in targets}, "rows": rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())