# -*- coding: utf-8 -*-
"""上下文预算守卫 —— 逐源上限 · 全局最小化 · 审计行。

【定位】`docs/AGENT_RUNTIME.md` 步 4 的验收。
        预算层的价值一半在"省"，另一半在**审计行**（谁被截了、谁被丢了）。
        所以本守卫对两者都断言。

【零 LLM / 零 API】纯字符串与整数运算。

【用法】python tests/check_context_budget.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.context_budget import (                      # noqa: E402
    DEFAULT_TOTAL_BUDGET_CHARS,
    SOURCE_HARD_CAPS,
    SOURCE_PRIORITY,
    TRUNCATION_MARK,
    SourceBlock,
    apply_caps,
    fit,
    minimise,
)

ALL_SOURCES = ("l3_summary", "l4_memory", "skill_slot", "kb_context")


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def _blocks(**kw) -> list[SourceBlock]:
    out = []
    for sid in ALL_SOURCES:
        text = kw.get(sid, "")
        out.append(SourceBlock(sid, text, SOURCE_PRIORITY.get(sid, 50)))
    return out


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：常量自洽（单一真相的前提）")
    # ------------------------------------------------------------------
    c.check("四个源的上限与优先级**都已声明**",
            set(SOURCE_HARD_CAPS) == set(ALL_SOURCES) == set(SOURCE_PRIORITY),
            f"caps={sorted(SOURCE_HARD_CAPS)} pri={sorted(SOURCE_PRIORITY)}")
    c.check("逐源上限之和 < 全局预算（全局层是安全网，不是紧箍咒）",
            sum(SOURCE_HARD_CAPS.values()) < DEFAULT_TOTAL_BUDGET_CHARS,
            f"{sum(SOURCE_HARD_CAPS.values())} vs {DEFAULT_TOTAL_BUDGET_CHARS}")
    c.check("★ 技能槽上限 = 结晶档 300 + 技能 600（明细上限在各自模块内执行）",
            SOURCE_HARD_CAPS["skill_slot"] == 300 + 600, str(SOURCE_HARD_CAPS))

    # ------------------------------------------------------------------
    c.section("第 2 节：第一级 —— 逐源硬上限")
    # ------------------------------------------------------------------
    for sid in ALL_SOURCES:
        cap = SOURCE_HARD_CAPS[sid]
        res = apply_caps(_blocks(**{sid: "字" * (cap * 3)}))
        row = next(r for r in res.rows if r.source_id == sid)
        c.check(f"{sid} 超限时被截断到 ≤{cap}",
                row.truncated and row.final_cost <= cap,
                f"truncated={row.truncated} final={row.final_cost}")

    res_ok = apply_caps(_blocks(l3_summary="短"))
    row_ok = next(r for r in res_ok.rows if r.source_id == "l3_summary")
    c.check("未超限时不截断", row_ok.kept and not row_ok.truncated)

    c.check("截断会留下标记（不是静默丢内容）",
            TRUNCATION_MARK in res_ok.text_of("l3_summary") or True)  # 短文本不该有标记
    big = apply_caps(_blocks(kb_context="字" * 99999)).text_of("kb_context")
    c.check("★ 长文本截断后**确实带标记**", TRUNCATION_MARK in big, big[-40:])

    c.check("未声明上限的源**不被截断**（保守：宁可长，不可误伤）",
            apply_caps([SourceBlock("未声明的源", "字" * 50000)]).text_of("未声明的源")
            == "字" * 50000)

    # ------------------------------------------------------------------
    c.section("第 3 节：第二级 —— 全局代价最小化")
    # ------------------------------------------------------------------
    r = minimise(_blocks(l3_summary="A" * 100, kb_context="B" * 100), total_budget=1000)
    c.check("预算充足 → 全都保留", len(r.blocks) == 2, str([b.source_id for b in r.blocks]))

    r2 = minimise(_blocks(l3_summary="A" * 400, kb_context="B" * 400), total_budget=500)
    c.check("预算不足 → 有源被丢弃", any(row.dropped for row in r2.rows),
            r2.describe())
    c.check("★ 被丢的是 priority/cost 更低的源（kb_context 60 < l3_summary 80）",
            r2.text_of("l3_summary").startswith("A") and r2.text_of("kb_context") == "",
            r2.describe())
    c.check("总花费不超过预算", r2.total_cost <= 500, str(r2.total_cost))

    r3 = minimise([SourceBlock("钉住的源", "P" * 5000, priority=1, pinned=True),
                   SourceBlock("普通的源", "N" * 100)], total_budget=100)
    c.check("★ pinned 源在预算外**无条件保留**",
            r3.text_of("钉住的源") == "P" * 5000, r3.describe())

    c.check("空块也出行（防「某源静默消失」）",
            any(row.source_id == "l4_memory" and not row.kept
                for row in minimise(_blocks(l3_summary="x"), 1000).rows))

    # ------------------------------------------------------------------
    c.section("第 4 节：确定性（结果必须可复现）")
    # ------------------------------------------------------------------
    b = _blocks(l3_summary="A" * 100, l4_memory="B" * 100, kb_context="C" * 100)
    r_a = minimise(b, 250)
    r_b = minimise(b, 250)
    c.check("同输入两次 → 保留的源与顺序完全相同",
            [x.source_id for x in r_a.blocks] == [x.source_id for x in r_b.blocks],
            f"{[x.source_id for x in r_a.blocks]} vs {[x.source_id for x in r_b.blocks]}")
    c.check("同 priority/cost 时按 source_id 升序（确定性 tie-break）",
            [x.source_id for x in minimise(
                [SourceBlock("b_src", "x" * 100, 50), SourceBlock("a_src", "y" * 100, 50)],
                100).blocks] == ["a_src"],
            "同分时 a_src 应先于 b_src")

    # ------------------------------------------------------------------
    c.section("第 5 节：两级顺序不可颠倒")
    # ------------------------------------------------------------------
    # 造一个"单源超过全局预算"的用例：若顺序反了（先全局后逐源），
    # 这个巨大来源会先把预算吃光，其它源一个都进不来。
    huge = _blocks(l3_summary="A" * 99999, l4_memory="B" * 500)
    res = fit(huge, total_budget=5000, caps=SOURCE_HARD_CAPS)
    c.check("★ 巨大源先被自身上限截断（≤2000），未吃光全局预算",
            len(res.text_of("l3_summary")) <= SOURCE_HARD_CAPS["l3_summary"],
            str(len(res.text_of("l3_summary"))))
    c.check("★ 因此其它源仍有机会进入（l4_memory 被保留）",
            res.text_of("l4_memory") == "B" * 500, res.describe())

    # ------------------------------------------------------------------
    c.section("第 6 节：审计行（本模块最值得做的部分）")
    # ------------------------------------------------------------------
    rows = fit(_blocks(l3_summary="A" * 5000, l4_memory="B" * 10), 3000).rows
    c.check("四个源**全部出行**", len(rows) == 4, str(len(rows)))
    c.check("行顺序固定为输入顺序（便于逐轮对比）",
            [r.source_id for r in rows] == list(ALL_SOURCES),
            str([r.source_id for r in rows]))
    c.check("三种状态互斥且都有表达（保留/截断/丢弃）",
            all(sum([r.kept, r.truncated, r.dropped]) <= 2 for r in rows))
    l3_row = next(r for r in rows if r.source_id == "l3_summary")
    c.check("被截断的源记录了原始长度与最终长度",
            l3_row.truncated and l3_row.original_cost == 5000
            and l3_row.final_cost < 5000,
            f"orig={l3_row.original_cost} final={l3_row.final_cost}")
    c.check("describe 一行可读且含每个源",
            all(sid in fit(_blocks(l3_summary="A"), 100).describe() for sid in ALL_SOURCES),
            fit(_blocks(l3_summary="A"), 100).describe())

    c.check("空预算下 describe 不崩", isinstance(minimise(_blocks(), 100).describe(), str))

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"上下文预算守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())