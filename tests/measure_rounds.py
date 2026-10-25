# -*- coding: utf-8 -*-
"""实测主链路工具循环的**轮次**与 **token** 分布 —— 为「轮次上限该定多少」提供依据。

【为什么需要这一步】`core/rag_engine.py:1095` 的 `max_loops = 3` 是**硬编码常量**，
而且它当初的依据是「3 轮的批处理够用」—— 那个判断**把「轮」当成了「次」**。
一轮内模型可以发**多个** `tool_calls`，所以 3 轮的实际容量远大于 3 次调用
（`docs/HANDOFF.md` 记录了那次被实测推翻的判断）。这个数字**从未被实测过**。

本脚本测三件事，用来判断「上限从 3 提到 8」值不值：

  ① **轮数分布** —— 每个问题实际用了几轮
  ② **撞墙率**（`wants_more`）—— 这是**唯一**能说明「3 轮不够」的证据
  ③ **token 分布**（总量 / 每轮均量）—— 用来估算提高上限的代价

【实验设计】问题集刻意覆盖不同形态：单工具 / 多工具 / 链式 / 批量 / 搜索 / 纯知识。
「搜索 + 分析」那类最可能撞墙，因为它要先搜、再判断要不要继续查。

【隔离】`owns_user_data=False`：不写 L4 记忆、不写会话存档；
同时桩掉标题生成与 L4 worker，避免它们的额外 LLM 调用污染计量。

【用法】
    python tests/measure_rounds.py                 # 全部问题
    python tests/measure_rounds.py --only 3        # 只跑前 3 个
    python tests/measure_rounds.py --model deepseek-v4-pro

【注意】本脚本会**真实调用 API 并产生费用**，因此**不登记在 `run_all.py` 里**，
与 `measure_cache.py` / `measure_dilution.py` / `measure_rerank_scores.py` 同类。
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.llm_client import Usage                     # noqa: E402
from core.rag_engine import MAX_TOOL_ROUNDS, RAGEngine  # noqa: E402

#: (标签, 问题) —— 标签只用于打印，不参与逻辑。
QUESTIONS: list[tuple[str, str]] = [
    ("单工具·统计", "阿克苏地区 2021 年的棉花产量是多少"),
    ("单工具·天气", "明天阿拉尔适合打药吗"),
    ("多工具·统计+绘图", "阿克苏近五年的棉花产量趋势，画个图"),
    ("链式·对比", "阿克苏地区的棉花产量比喀什地区高多少"),
    ("批量·多地区", "把新疆主要产棉地州 2021 年的棉花产量都查一遍"),
    ("搜索+分析", "查一下最近新疆棉花目标价格补贴政策有什么变化，再说说对种植决策的影响"),
    ("纯知识", "棉花苗期需要怎么施肥"),
]


def _prep(engine: RAGEngine) -> None:
    """桩掉会额外花 LLM 调用的部分，保证计量只反映问答本身。"""
    engine._generate_title = lambda *a, **k: None            # type: ignore[method-assign]
    engine._start_memory_worker = lambda *a, **k: None       # type: ignore[method-assign]


def _reset_loop_attrs(engine: RAGEngine) -> None:
    """清掉上一题的循环观测值。

    ★ 必须显式清：不走工具循环的问题（如纯知识问答）**不会重写**这三个属性，
      不清就会读到上一题的脏值。
    """
    engine._last_loop_wants_more = False                     # type: ignore[attr-defined]
    engine._last_loop_rounds = 0                             # type: ignore[attr-defined]
    engine._last_loop_completed = []                         # type: ignore[attr-defined]


def _delta(before: Usage, after: Usage) -> Usage:
    """两次快照之差 —— 只反映本题的开销。"""
    return Usage(
        prompt_tokens=after.prompt_tokens - before.prompt_tokens,
        completion_tokens=after.completion_tokens - before.completion_tokens,
        cache_hit_tokens=after.cache_hit_tokens - before.cache_hit_tokens,
        cache_miss_tokens=after.cache_miss_tokens - before.cache_miss_tokens,
        calls=after.calls - before.calls,
    )


def _tool_names(completed: list) -> list[str]:
    """从 `completed_calls()` 的产出里抽出工具名（结构容错，抽不到就返回空）。"""
    names: list[str] = []
    for item in completed or []:
        if isinstance(item, dict):
            n = item.get("name") or item.get("tool")
            if n:
                names.append(str(n))
        elif isinstance(item, str):
            names.append(item)
    return names


def main() -> int:
    args = sys.argv[1:]

    def _opt(flag: str) -> str:
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return ""

    only = int(_opt("--only") or 0)
    model_override = _opt("--model")
    questions = QUESTIONS[:only] if only > 0 else QUESTIONS

    print("=" * 80)
    print("主链路工具循环 —— 轮次与 token 实测")
    print("=" * 80)

    engine = RAGEngine(owns_user_data=False)
    _prep(engine)
    if model_override:
        engine.llm.model = model_override             # type: ignore[attr-defined]

    provider = os.getenv("LLM_PROVIDER", "?")
    model = getattr(engine.llm, "model", "?")
    print(f"  provider={provider}  model={model}"
          f"  MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS}")
    print(f"  问题数={len(questions)}")
    print("=" * 80)

    engine.create_session()

    rows: list[tuple[str, str, int, bool, list[str], Usage]] = []

    for i, (tag, q) in enumerate(questions, 1):
        _reset_loop_attrs(engine)
        before: Usage = engine.llm.usage_total
        engine.llm.reset_usage()
        engine.llm.usage_total = before                # 只清 last，保留累计

        print(f"\n  [{i}/{len(questions)}] {tag}")
        print(f"        问题: {q}")

        try:
            for _ in engine.ask(q):
                pass
        except Exception as e:                          # noqa: BLE001
            print(f"        ★ 异常: {type(e).__name__}: {e}")
            continue

        rounds = int(getattr(engine, "_last_loop_rounds", 0) or 0)
        wall = bool(getattr(engine, "_last_loop_wants_more", False))
        tools = _tool_names(getattr(engine, "_last_loop_completed", []))
        delta = _delta(before, engine.llm.usage_total)
        rows.append((tag, q, rounds, wall, tools, delta))

        print(f"        轮数: {rounds}    工具: {', '.join(tools) or '（无）'}"
              f"    撞墙: {'★是' if wall else '否'}")
        print(f"        用量: {delta.describe()}")

    # ---------------- 汇总 ----------------
    print()
    print("=" * 80)
    print("汇总")
    print("=" * 80)

    if not rows:
        print("  没有成功完成的问题。")
        return 1

    dist = Counter(r[2] for r in rows)
    total = Usage()
    for r in rows:
        total = total + r[5]

    used_loop = [r for r in rows if r[2] > 0]
    total_rounds = sum(r[2] for r in used_loop)
    wall_rows = [r for r in rows if r[3]]
    all_tok = total.prompt_total + total.completion_tokens
    per_round = (all_tok // total_rounds) if total_rounds else 0

    print("  轮数分布:")
    for k in sorted(dist):
        label = f"{k} 轮" if k else "未走工具循环"
        print(f"    {label:<14} × {dist[k]}")
    print()
    print(f"  撞墙（wants_more）    {len(wall_rows)}/{len(rows)}"
          f" = {len(wall_rows) / len(rows):.1%}")
    print(f"  token 总计            {all_tok}"
          f"  (prompt {total.prompt_total} + completion {total.completion_tokens})")
    print(f"  每问平均              {all_tok // len(rows)}")
    print(f"  每轮平均              {per_round}  (共 {total_rounds} 轮)")
    print(f"  工具调用总数          {len([t for r in rows for t in r[4]])}")

    if wall_rows:
        wall_usage = Usage()
        for r in wall_rows:
            wall_usage = wall_usage + r[5]
        wall_tok = wall_usage.prompt_total + wall_usage.completion_tokens
        print()
        print("  ★ 撞墙的那几个（提上限的受益者）:")
        for tag, _q, rounds, _w, tools, u in wall_rows:
            print(f"    {tag:<16} {rounds} 轮  "
                  f"{u.prompt_total + u.completion_tokens} tok  "
                  f"工具={', '.join(tools) or '（无）'}")
        print(f"    撞墙任务平均 {wall_tok // len(wall_rows)} tok"
              f"（全局平均 {all_tok // len(rows)} tok）")
        print()
        print("  ★ 上限从 3 提到 8 的代价估算:")
        print(f"    按每轮均量 {per_round} tok 算，撞墙任务最多再补 5 轮"
              f" ≈ 多花 {per_round * 5} tok/题")
        print(f"    影响面 {len(wall_rows)}/{len(rows)}"
              f" = {len(wall_rows) / len(rows):.1%} 的问题")
    else:
        print()
        print("  ⇒ 本轮**没有任务撞墙** —— 按这批问题看，3 轮够用，")
        print("    提高上限的收益无法从这份数据里得到支持。")
        print("    （注意：问题集只有 %d 题，且都是常规形态；结论不宜外推。）" % len(rows))

    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())