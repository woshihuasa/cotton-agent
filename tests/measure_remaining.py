# -*- coding: utf-8 -*-
"""判定「剩余工作是否可枚举」—— 只看数据，不生成任何节点。

【要判定什么】任务跑到一半停下时，剩下的活分两种：

  · **可枚举**：剩下那几步的**参数现在就已经知道**。
    例：用户要查 14 个地州，主链路查了 11 个 —— 剩下 3 个是哪 3 个，
    从「数据里全部地区」减掉「已完成的」就算出来了。

  · **不可枚举**：下一步取决于还没算出的结果。
    例：「先看产量，低了再查病虫害」—— 而且引擎没有条件分支节点（`needs` 只表达依赖）。

【判定的两步】
  ① **找重复模式**：同一工具被调 N 次，且**只有一个参数在变**（其余恒定）。
  ② **解析值域**：那个在变的参数，取值范围能不能从**现成的地方**读到 ——
     工具 schema 的 `enum`，或数据文件（统计 CSV 的地区列 / 年份列）。

【为什么第 ② 步是成败关键】值域来自**数据**就稳；只能从用户那句话里猜就不该做 ——
词面判定是这个项目栽过跟头的地方（`core/perception/intent.py` 的注释记了两次）。

【不做的事】本脚本**不生成节点、不启动工作流、不写文件**。只打印判定结果。

【用法】python tests/measure_remaining.py [--only N]
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.rag_engine import RAGEngine                       # noqa: E402
from core.tools.schemas import TOOL_SCHEMAS                  # noqa: E402

#: 刻意设计成**会产生重复调用**的问题 —— 判定逻辑只在有重复时才可能命中。
QUESTIONS: list[tuple[str, str]] = [
    ("批量·地区", "把新疆主要产棉地州 2021 年的棉花产量都查一遍"),
    ("批量·年份", "阿克苏近五年的棉花产量趋势，画个图"),
    ("链式·对比", "对比阿克苏、喀什、和田、巴州、克州五个地区的棉花产量"),
    ("单次", "阿克苏地区 2021 年的棉花产量是多少"),
    ("搜索", "查一下最近新疆棉花目标价格补贴政策有什么变化"),
]

_CSV = ROOT / "data" / "新疆_数据统计" / "新疆棉花产量年度统计.csv"


# ---------------------------------------------------------------- 值域解析
def _csv_column(col_name: str) -> list[str]:
    """从统计 CSV 读某一列的全部去重取值（保序）。"""
    if not _CSV.exists():
        return []
    out: list[str] = []
    with _CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            v = (row.get(col_name) or "").strip()
            if v and v not in out:
                out.append(v)
    return out


def resolve_domain(tool: str, param: str) -> tuple[list[str], str] | None:
    """解析参数的值域。返回 (值域, 来源说明)；解析不到返回 None。"""
    # ① 工具 schema 自己声明的 enum —— 最权威
    for t in TOOL_SCHEMAS:
        fn = t.get("function", t)
        if fn.get("name") != tool:
            continue
        spec = ((fn.get("parameters") or {}).get("properties") or {}).get(param)
        if isinstance(spec, dict) and spec.get("enum"):
            vals = [str(v) for v in spec["enum"]]
            return vals, f"工具 schema 的 enum（{len(vals)} 个）"

    # ② 数据文件里的现成列
    if param in ("region", "regions", "city"):
        vals = _csv_column("地区")
        if vals:
            return vals, f"统计 CSV 的「地区」列（{len(vals)} 个）"
    if param in ("year", "start", "end", "target_year"):
        vals = _csv_column("年份")
        if vals:
            return vals, f"统计 CSV 的「年份」列（{len(vals)} 个）"

    # ③ 自由文本类参数 —— 明确不可枚举
    return None


# ---------------------------------------------------------------- ① 找重复模式
def detect_patterns(done: list[dict]) -> list[dict]:
    """同一工具被调 ≥2 次、且只有一个参数在变 → 一条候选模式。"""
    by_tool: dict[str, list[dict]] = {}
    for call in done or []:
        name = str(call.get("name") or "")
        args = call.get("args")
        if name and isinstance(args, dict):
            by_tool.setdefault(name, []).append(args)

    patterns: list[dict] = []
    for name, arg_list in by_tool.items():
        if len(arg_list) < 2:
            continue
        keys: set[str] = set()
        for a in arg_list:
            keys |= set(a.keys())

        def _val(a: dict, k: str) -> str:
            try:
                return json.dumps(a.get(k), ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                return str(a.get(k))

        varying = [k for k in sorted(keys)
                   if len({_val(a, k) for a in arg_list}) > 1]
        const = [k for k in sorted(keys) if k not in varying]
        if len(varying) == 1:
            patterns.append({
                "tool": name, "varying": varying[0], "const_keys": const,
                "n_calls": len(arg_list),
                "values": [_val(a, varying[0]).strip('"') for a in arg_list],
            })
    return patterns


# ---------------------------------------------------------------- 驱动
def _prep(engine: RAGEngine) -> None:
    engine._generate_title = lambda *a, **k: None            # type: ignore[method-assign]
    engine._start_memory_worker = lambda *a, **k: None       # type: ignore[method-assign]


def _reset_loop_attrs(engine: RAGEngine) -> None:
    engine._last_loop_wants_more = False                     # type: ignore[attr-defined]
    engine._last_loop_rounds = 0                             # type: ignore[attr-defined]
    engine._last_loop_completed = []                         # type: ignore[attr-defined]


def main() -> int:
    args = sys.argv[1:]
    only = 0
    if "--only" in args and args.index("--only") + 1 < len(args):
        only = int(args[args.index("--only") + 1])
    questions = QUESTIONS[:only] if only > 0 else QUESTIONS

    print("=" * 92)
    print("判定「剩余工作是否可枚举」—— 只看数据，不生成节点")
    print("=" * 92)

    engine = RAGEngine(owns_user_data=False)
    _prep(engine)
    engine.create_session()

    stats = {"可枚举": 0, "有重复但值域解析不到": 0, "没有重复模式": 0}

    for i, (tag, q) in enumerate(questions, 1):
        _reset_loop_attrs(engine)
        print()
        print("-" * 92)
        print(f"  [{i}/{len(questions)}] {tag} —— {q}")
        print("-" * 92)
        try:
            for _ in engine.ask(q):
                pass
        except Exception as e:                              # noqa: BLE001
            print(f"        ★ 异常: {type(e).__name__}: {e}")
            continue

        done = list(getattr(engine, "_last_loop_completed", []) or [])
        print(f"        完成调用 {len(done)} 次: "
              f"{[c.get('name') for c in done]}")

        patterns = detect_patterns(done)
        if not patterns:
            print("        ① 重复模式：**无**（同一工具没有只变一个参数的多次调用）")
            stats["没有重复模式"] += 1
            continue

        for p in patterns:
            print(f"        ① 重复模式命中：`{p['tool']}` 调了 {p['n_calls']} 次，"
                  f"只有 `{p['varying']}` 在变")
            print(f"           已用取值: {p['values'][:6]}"
                  f"{' …' if len(p['values']) > 6 else ''}")
            print(f"           恒定参数: {p['const_keys']}")

            dom = resolve_domain(p["tool"], p["varying"])
            if dom is None:
                print("        ② 值域：**解析不到** —— 无法枚举，只能存档")
                stats["有重复但值域解析不到"] += 1
                continue

            vals, source = dom
            used = set(p["values"])
            remaining = [v for v in vals if v not in used and v != "总计"]
            print(f"        ② 值域：**可枚举** ← {source}")
            print(f"           值域 {len(vals)} 个，已用 {len(used)} 个，"
                  f"**剩余 {len(remaining)} 个**")
            print(f"           剩余示例: {remaining[:6]}"
                  f"{' …' if len(remaining) > 6 else ''}")
            stats["可枚举"] += 1

    print()
    print("=" * 92)
    print("汇总")
    print("=" * 92)
    for k, v in stats.items():
        print(f"  {k:<22} {v}/{len(questions)}")
    print()
    print("  注：本脚本只判定，不生成节点、不启动工作流。")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())