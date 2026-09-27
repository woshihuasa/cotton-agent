# -*- coding: utf-8 -*-
"""
Rerank 分数分布测量（Similarity Threshold Calibration）
========================================================

【用途】
  为"相似度阈值过滤"提供**数据依据**，避免拍脑袋定阈值。

  做法：对检索集中每条 query 召回 top-N 候选，逐条用 Rerank 打分，
  再按"是否为期望文档"分成两组统计分数分布：

    · 正样本组：期望文档（人工标注为正确答案）
    · 负样本组：同批召回中的非期望文档

  判读：若两组**分离良好**（正样本分数显著高于负样本），则存在安全阈值区间；
  若**重叠严重**，任何阈值都会误伤正确答案 —— 此时应放弃该方案。

【输出】
  1. 两组的分数分布：min / p25 / 中位数 / p75 / max
  2. 分桶直方图（0.1 一档）
  3. **建议阈值区间**：正样本最低分 ～ 负样本最高分
  4. 若指定 --threshold：报告过滤条数、误伤的正样本数、以及过滤后剩余上下文条数

【实现说明】
  本脚本**自带 Rerank 调用**（不依赖 core.knowledge_base 的精排实现），
  以保证测量工具与被测系统的产线代码相互独立。

【用法】
  .venv\\Scripts\\python.exe tests\\measure_rerank_scores.py
  .venv\\Scripts\\python.exe tests\\measure_rerank_scores.py --threshold 0.5
  .venv\\Scripts\\python.exe tests\\measure_rerank_scores.py --k 20 --threshold 0.3

【成本】
  每条 1 次 Embedding + 1 次 Rerank（均为免费档模型），与检索评测同量级。
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

import requests                                    # noqa: E402

from config import AppConfig                       # noqa: E402
from core.knowledge_base import KnowledgeBase      # noqa: E402

DATASET = os.path.join("tests", "eval_data", "retrieval_set.jsonl")

# ── chunk 级答案特征词表（口径与打分粒度对齐）────────────────────
# 用于 --chunk-level 模式：chunk 命中任一关键词 → 判为「真正样本」。
# 设计要点：
#   · 词取自**文档正文的实际表述**（而非题面用语），例如 #21 用「棉叶螨」而非"红蜘蛛"
#   · 每条的词应覆盖"能证明该段回答了问题"的最小特征集
#   · 该表为**人工维护**，修改后重跑即可复核阈值可行性
ANSWER_KEYWORDS: dict[int, list[str]] = {
    1:  ["整地", "播前", "耙"],
    2:  ["复合肥", "基肥"],
    3:  ["缩节胺", "化控"],
    4:  ["陆地棉", "品种", "南疆"],
    5:  ["长绒棉", "品种"],
    6:  ["滴水量", "灌溉定额", "全生育期"],
    7:  ["出苗水", "盐碱"],
    8:  ["隔沟", "交替灌溉"],
    9:  ["相对含水量", "含水量低于"],
    10: ["黄淮海", "施肥"],
    11: ["有机质", "有机肥"],
    12: ["移栽", "密度"],
    13: ["脱叶", "催熟", "温度"],
    14: ["脱叶率", "吐絮率"],
    15: ["早衰", "后期"],
    16: ["高温", "叶面施肥", "叶面喷施"],
    17: ["干播湿出", "播种"],
    18: ["用种量", "播种量"],
    19: ["蓟马", "棉蚜", "防治"],
    20: ["棉铃虫", "诱杀", "食诱剂"],
    21: ["棉叶螨", "全田防治", "15%"],
    22: ["杂草", "防除", "种类"],
    23: ["防控原则", "综合防治", "杂草"],
    24: ["自育", "品种", "占比"],
    25: ["产量", "品质", "高质量发展"],
    26: ["价格指数", "等级", "品级"],
    27: ["统计口径", "统计范围", "产量"],
}


def hit_keywords(text: str, kws: list[str]) -> bool:
    """chunk 正文是否命中该 query 的任一答案特征词（chunk 级口径）。"""
    return any(kw in text for kw in kws) if kws else False


def doc_name(doc) -> str:
    try:
        return Path(doc.metadata.get("source", "") or "").name
    except AttributeError:
        return ""


def load_cases() -> list[dict]:
    cases = []
    with open(DATASET, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def score_docs(query: str, texts: list[str]) -> list[float]:
    """调用 Rerank API 逐条打分，返回与 texts 同序的分数列表（失败返回全 0）。"""
    if not texts:
        return []
    try:
        resp = requests.post(
            AppConfig.EMBEDDING_BASE_URL.rstrip("/") + "/rerank",
            headers={"Authorization": "Bearer " + AppConfig.EMBEDDING_API_KEY},
            json={"model": AppConfig.RERANK_MODEL, "query": query,
                  "documents": [t[:2000] for t in texts],
                  "top_n": len(texts), "return_documents": False},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        scores = [0.0] * len(texts)
        for r in results:
            idx = r.get("index")
            if isinstance(idx, int) and 0 <= idx < len(texts):
                scores[idx] = float(r.get("relevance_score", 0.0))
        return scores
    except Exception as e:
        print(f"  [警告] 打分失败: {e}")
        return [0.0] * len(texts)


def pct(values: list[float], q: float) -> float:
    """简单分位数（线性插值）。"""
    if not values:
        return 0.0
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    pos = q * (len(vs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def describe(name: str, values: list[float]) -> None:
    if not values:
        print(f"  {name}: （无样本）")
        return
    print("  %-8s n=%-4d min=%.4f  p25=%.4f  p50=%.4f  p75=%.4f  max=%.4f"
          % (name, len(values), min(values), pct(values, 0.25),
             pct(values, 0.5), pct(values, 0.75), max(values)))


def histogram(name: str, values: list[float]) -> None:
    if not values:
        return
    buckets = [0] * 10
    for v in values:
        buckets[min(int(v * 10), 9)] += 1
    print(f"\n  {name} 分布（每档 0.1）:")
    total = len(values)
    for i, c in enumerate(buckets):
        if c == 0:
            continue
        lo, hi = i / 10, (i + 1) / 10
        bar = "█" * max(1, int(c / total * 40))
        print("    [%.1f, %.1f) %-4d %s" % (lo, hi, c, bar))


def main() -> None:
    ap = argparse.ArgumentParser(description="Rerank 分数分布测量（阈值定标）")
    ap.add_argument("--k", type=int, default=20, help="每次召回的候选条数（默认 20）")
    ap.add_argument("--threshold", type=float, default=None,
                    help="给定阈值，报告过滤效果与误伤情况")
    ap.add_argument("--chunk-level", action="store_true",
                    help="用 chunk 级口径判定正/负样本（命中答案特征词才算正样本）；"
                         "不加则用文档级口径（属于期望文档即算正样本）")
    args = ap.parse_args()

    cases = load_cases()
    kb = KnowledgeBase()
    level = "chunk 级（命中答案特征词）" if args.chunk_level else "文档级（属于期望文档）"
    print("Rerank 分数分布测量：%d 条 query ｜ 候选 top-%d ｜ 模型 %s"
          % (len(cases), args.k, AppConfig.RERANK_MODEL))
    print("判定口径：%s\n" % level)

    pos: list[float] = []          # 正样本分数
    neg: list[float] = []          # 负样本分数
    per_query = []                 # [(cid, query, [(score, is_pos)])]

    for case in cases:
        query, expected = case["query"], set(case["expected_docs"])
        kws = ANSWER_KEYWORDS.get(case["id"], [])
        pool = kb._vector_store.similarity_search(query, k=args.k)
        if not pool:
            continue
        scores = score_docs(query, [d.page_content for d in pool])
        rows = []
        for d, s in zip(pool, scores):
            if args.chunk_level:
                # chunk 级口径：以正文是否含答案特征词判定（与打分粒度一致）
                is_pos = hit_keywords(d.page_content, kws)
            else:
                # 文档级口径：是否属于期望文档（在 chunk 级打分下会失真）
                is_pos = doc_name(d) in expected
            rows.append((s, is_pos))
            (pos if is_pos else neg).append(s)
        per_query.append((case["id"], query, rows))
        print("  #%-3d 正样本 %s 条 ｜ 正样本最高分 %.4f ｜ 负样本最高分 %.4f"
              % (case["id"], sum(1 for _, e in rows if e),
                 max([s for s, e in rows if e], default=0.0),
                 max([s for s, e in rows if not e], default=0.0)))

    print("\n" + "=" * 70)
    print("【分数分布】")
    describe("正样本", pos)
    describe("负样本", neg)
    histogram("正样本", pos)
    histogram("负样本", neg)

    print("\n" + "=" * 70)
    print("【建议阈值区间】")
    if pos and neg:
        lo, hi = min(pos), max(neg)
        if lo > hi:
            print("  ✅ 分离良好：阈值可取 (%.4f, %.4f] 之间的任意值" % (hi, lo))
            print("     （正样本最低分 %.4f > 负样本最高分 %.4f，间隙 %.4f）" % (lo, hi, lo - hi))
        else:
            print("  ⚠️ 两组重叠（正样本最低分 %.4f ≤ 负样本最高分 %.4f）" % (lo, hi))
            print("     → 任何阈值都会误伤部分正确答案；需权衡「上下文洁净度」与「召回损失」")
        mis = [v for v in pos if v < hi]
        print("     若阈值取负样本最高分 %.4f：正样本会被误伤 %d/%d 条（%.1f%%）"
              % (hi, len(mis), len(pos), len(mis) / len(pos) * 100))

    if args.threshold is not None:
        t = args.threshold
        print("\n" + "=" * 70)
        print("【阈值 %.4f 的实际影响】" % t)
        killed_pos = sum(1 for v in pos if v < t)
        killed_neg = sum(1 for v in neg if v < t)
        print("  过滤掉：正样本 %d/%d 条（误伤）｜ 负样本 %d/%d 条（清理）"
              % (killed_pos, len(pos), killed_neg, len(neg)))
        print("  上下文平均条数：过滤前 %.2f → 过滤后 %.2f"
              % (args.k, sum(1 for _, _, rows in per_query
                             for s, _ in rows if s >= t) / max(len(per_query), 1)))
        empty = [cid for cid, _, rows in per_query if all(s < t for s, _ in rows)]
        if empty:
            print("  ⚠️ 以下 query 的全部候选都低于阈值（上下文为空 → 可触发拒答）：%s" % empty)
        else:
            print("  ✅ 没有 query 出现「全部候选低于阈值」的情况")


if __name__ == "__main__":
    main()
