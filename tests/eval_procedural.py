# -*- coding: utf-8 -*-
"""技能库检索质量评测 —— 20 条"问题 → 期望路线" + 4 条已知边界。

【定位】与两个守卫的分工：
        · `check_procedural_memory.py`  守技能库**自身**（门禁、去重、持久化）
        · `check_skill_wiring.py`       守**接线**（技能段真注入、轨迹真登记）
        · **本脚本**                    量**检索质量**（问句能否召回正确的路线）

【口径】
        · **Hit@2 是主口径** —— 生产链路注入的就是 top-2，召回不到就等于没用。
        · Hit@1 作为精度参考（top-2 都会注入，所以 top-1 错不致命，但退化要能看见）。
        · 未命中按 `eval_retrieval.py` 的既有做法**归因**：目标路线根本不在候选里 = 召回问题；
          在候选里但名次靠后 = 排序问题。

【已知边界不计入分母】标 `boundary: true` 的 4 条是"口语化但不含任何标签词"的问句，
        当前**本就应当不命中**（这类问题模型靠知识库就能答，不需要经验提示）。
        它们**故意不做断言** —— 若将来检索变强、开始命中，属于改进而非回归，
        届时应把该条移入评分集，而不是让守卫拦住它。

【零 LLM / 零费用】技能库不依赖 embedding 与大模型，全程确定性可复现。

【⚠️ 这是回归快照，不是独立质量度量】用例由本仓库自行设计，因此 100% 属于**预期结果**，
        不代表对真实农户问句的泛化能力。它的作用是**锁住当前行为** —— 日后改动触发器、
        排序规则或词典，若数字下降会被立刻看见。真实泛化能力需要口语化问句集（含干扰项），
        那属于后续工作（ROADMAP「评测集扩充」同源）。

【用法】python tests/eval_procedural.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.procedural_memory import ProceduralMemoryStore   # noqa: E402

DATASET = ROOT / "tests" / "eval_data" / "procedural_set.jsonl"
SANDBOX = ROOT / "tests" / "eval_outputs" / "procedural_eval_sandbox"

# 门槛：生产注入 top-2，故 Hit@2 是主口径；允许 20 条里错 2 条。
TARGET_HIT2 = 0.90
TARGET_HIT1 = 0.75


def load_rows() -> list[dict]:
    rows: list[dict] = []
    for line in DATASET.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)

    rows = load_rows()
    seeds = [r for r in rows if r.get("type") == "seed"]
    cases = [r for r in rows if r.get("type") == "retrieve"]
    if len(seeds) != 1:
        print(f"[FAIL] 期望恰好 1 行 seed，实际 {len(seeds)}")
        return 1

    # 用**真实登记路径**建种子（走门禁，不绕过）
    store = ProceduralMemoryStore(SANDBOX / "procedural_memory" / "index.jsonl")
    for e in seeds[0].get("entries", []):
        store.record_trajectory(e["chain"], e.get("arg_shape"), triggers=e.get("triggers"))

    scored = [c for c in cases if not c.get("boundary")]
    boundary = [c for c in cases if c.get("boundary")]

    print("=" * 78)
    print(f"技能库检索评测：种子 {store.counts()['entries']} 条路线"
          f" | 评分用例 {len(scored)} 条 | 已知边界 {len(boundary)} 条")
    print("=" * 78)

    hit1 = hit2 = 0
    rank_miss: list[dict] = []      # 排序问题
    recall_miss: list[dict] = []    # 召回问题

    for c in scored:
        got = [e["signature"] for e in store.match(c["question"], k=2)]
        expect = c["expect_any"]
        ok1 = bool(got) and got[0] in expect
        ok2 = any(s in expect for s in got)
        hit1 += int(ok1)
        hit2 += int(ok2)

        if ok2 and not ok1:
            rank_miss.append({"id": c["id"], "got": got, "expect": expect})
        elif not ok2:
            # 目标路线是否出现在更宽的候选里？用来区分排序/召回
            wider = [e["signature"] for e in store.match(c["question"], k=20)]
            (rank_miss if any(s in expect for s in wider) else recall_miss).append(
                {"id": c["id"], "got": got, "expect": expect, "wider": wider}
            )

        mark = "OK  " if ok2 else "FAIL"
        flag = "" if ok1 else "  (top1 错)"
        print(f"  [{mark}] #{c['id']:<4} {c['question'][:26]:<28} → {got}{flag}")

    n = len(scored)
    r1 = hit1 / n if n else 0.0
    r2 = hit2 / n if n else 0.0

    print()
    if rank_miss:
        print("  未命中归因【排序问题】——目标在候选池内但名次靠后:")
        for m in rank_miss:
            print(f"    · #{m['id']} 期望 {m['expect']} 实得 {m['got']}")
    if recall_miss:
        print("  未命中归因【召回问题】——目标完全没进候选:")
        for m in recall_miss:
            print(f"    · #{m['id']} 期望 {m['expect']} 实得 {m['got']}")

    print()
    print("  已知边界（不计入分母，故意不断言）:")
    for c in boundary:
        got = [e["signature"] for e in store.match(c["question"], k=2)]
        print(f"    [INFO] #{c['id']:<4} {c['question'][:26]:<28} → {got or '（未命中）'}")

    print()
    print("=" * 78)
    print(f"Hit@1: {hit1}/{n} = {r1 * 100:.1f}%   （参考门槛 ≥ {TARGET_HIT1:.0%}）")
    print(f"Hit@2: {hit2}/{n} = {r2 * 100:.1f}%   （**主口径**，门槛 ≥ {TARGET_HIT2:.0%}）")
    passed = r1 >= TARGET_HIT1 and r2 >= TARGET_HIT2
    print("结论:", "达标 ✓" if passed else "未达标 ✗")
    print("=" * 78)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())