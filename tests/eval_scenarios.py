# -*- coding: utf-8 -*-
"""产物级场景评测 —— 断言「机制是否触发」，而非「答案像不像」。

【定位】`docs/AGENT_RUNTIME.md` 步 8 的验收，也是本项目**最弱一块**（评测）的补强。

【★ 一个必须先说清的边界：为什么不在这里断言"工具选得对不对"】

本评测用**脚本化 LLM**（`tests/harness.py`），工具是脚本指定的。
所以拿它来断言"模型挑对了工具"是**循环论证** —— 那是脚本决定的，不是引擎决定的。

因此本评测断言的是**产物与机制触发**：

  · 工具结果是否真的**回灌进消息**（证明循环把结果交给了模型）
  · 分级网关的档位是否被**遵守**（L0 时工具循环**一次都不跑**、零 embedding）
  · 能力闸门是否真的**拦住**（且挡住时工具实现**没跑**）
  · 工具出错时轨迹是否**不进结晶档**（G2 门禁）
  · 预算层是否**截断**（system prompt 确实变短了）

真实"工具选择"由付费评测覆盖（`eval_tools.py` / `eval_skill_acceptance.py`）。

【一处与原方案的偏离（如实记录）】
原计划另有 `tests/eval_data/scenarios.jsonl`。实际写下来，场景携带的是
**代码级期望**（运行模式 / 脚本化工具轮次 / 上下文长度），JSONL 表达不了，
硬塞就得先造一门小 DSL —— 那会让"可读的用例"变成"要解释的格式"。
故场景直接定义在 Python 里；数据驱动的部分（问题→期望工具）已在
`eval_skill_acceptance.py` 的 `acceptance` 字段里，不必重复一份。

【零 LLM / 零 API / 零网络】

【用法】python tests/eval_scenarios.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from harness import Harness                                  # noqa: E402


@dataclass
class Scenario:
    name: str
    question: str
    tool_turns: list = field(default_factory=list)
    web_mode: bool = False
    kb_context: str = ""
    l4_facts: list = field(default_factory=list)

    expect_route: str | None = None
    expect_tools: list = field(default_factory=list)
    forbid_tools: list = field(default_factory=list)
    expect_refused: list = field(default_factory=list)
    min_loop_rounds: int | None = None
    max_loop_rounds: int | None = None
    expect_kb_calls: int | None = None
    expect_l4_calls: int | None = None
    expect_crystallized_empty: bool = False
    max_system_chars: int | None = None
    expect_fed_back: str | None = None


SCENARIOS: list[Scenario] = [
    # ---- 1. 工具执行与**结果回灌**（机制）----
    Scenario(
        name="工具执行 → 结果回灌进消息",
        question="今天阿拉尔适合打药吗",
        tool_turns=[[("get_weather", {"city": "阿拉尔", "date": "today"})]],
        expect_route="L1",
        expect_tools=["get_weather"],
        min_loop_rounds=1,
        expect_fed_back="get_weather",
    ),
    # ---- 2. L0：全跳过 ----
    Scenario(
        name="L0 寒暄 → 工具循环一次都不跑、零 embedding",
        question="你好",
        expect_route="L0",
        forbid_tools=["get_weather", "plot_trend"],
        max_loop_rounds=0,
        expect_kb_calls=0,
        expect_l4_calls=0,
    ),
    # ---- 3. L1 检索型：跳过工具循环，但仍检索 ----
    Scenario(
        name="L1 检索型 → 跳过工具循环，检索照跑",
        question="棉花苗期需要怎么施肥",
        expect_route="L1",
        max_loop_rounds=0,
        expect_kb_calls=1,
        expect_l4_calls=1,
    ),
    # ---- 4. ★ 能力闸门：Web 模式拦住写盘工具 ----
    Scenario(
        name="★ 闸门：Web 模式拦住会写盘的 plot_trend",
        question="画一下棉花价格的走势图",
        tool_turns=[[("plot_trend", {"metric": "price"})]],
        web_mode=True,
        expect_refused=["plot_trend"],
        forbid_tools=["plot_trend"],
        expect_crystallized_empty=True,     # 被拒 = 工具出错 → G2 门禁不放行
    ),
    # ---- 5. 桌面模式放行同一脚本（证明闸门不是一刀切）----
    Scenario(
        name="桌面模式放行 plot_trend（闸门不是一刀切）",
        question="画一下棉花价格的走势图",
        tool_turns=[[("plot_trend", {"metric": "price"})]],
        web_mode=False,
        expect_tools=["plot_trend"],
    ),
    # ---- 6. 多轮收敛（机制真的走了多轮）----
    Scenario(
        name="多轮：两轮工具调用都执行",
        question="阿拉尔的天气和棉花产量统计给我看看",
        tool_turns=[
            [("get_weather", {"city": "阿拉尔", "date": "today"})],
            [("query_cotton_stats", {"metric": "area"})],
        ],
        min_loop_rounds=2,
        expect_tools=["get_weather", "query_cotton_stats"],
    ),
    # ---- 7. 预算截断：超长背景知识确实被截短 ----
    Scenario(
        name="预算：超长背景知识被截断（system prompt 明显变短）",
        question="棉花价格波动大吗",
        kb_context="这是一段会被预算层截断的背景知识。" * 600,   # ~9600 字，远超 3000 上限
        tool_turns=[[("calc_price_volatility", {"metric": "volatility"})]],
        expect_tools=["calc_price_volatility"],
        max_system_chars=9000,
    ),
    # ---- 8. L2：两类信号同时出现 → 全链（L4 与知识库都参与）----
    Scenario(
        name="L2 全链：工具 + 检索信号同时出现，L4 与知识库都跑",
        question="棉花苗期怎么施肥，再画个产量趋势图",       # 「怎么」+「画个/趋势图」→ L2
        l4_facts=["用户主栽塔河2号", "用户有100亩棉田"],
        tool_turns=[[("plot_trend", {"metric": "price"})]],
        expect_route="L2",
        expect_kb_calls=1,
        expect_l4_calls=1,
        expect_tools=["plot_trend"],
    ),
]


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"    [{'OK  ' if ok else 'FAIL'}] {desc}"
              + (f"   ← {detail}" if detail and not ok else ""))
        return ok


def _run_one(sc: Scenario, c: Checker, idx: int) -> None:
    h = Harness(
        # ⚠️ 两者是**反的**：Web 模式 = **不**拥有用户数据。
        # （写这段时先是直接把 web_mode 传了进去，被自己的评测当场抓出反断言。）
        owns_user_data=not sc.web_mode,
        sandbox_name=f"scenario_{idx}",
        kb_context=sc.kb_context,
        l4_facts=sc.l4_facts,
    )
    try:
        r = h.run(sc.question, tool_turns=sc.tool_turns)
    finally:
        h.close()

    if sc.expect_route is not None:
        c.check(f"档位 = {sc.expect_route}", r.route == sc.expect_route, r.route)

    for t in sc.expect_tools:
        c.check(f"工具 {t} 被**执行**", t in r.tools_called, str(r.tools_called))
    for t in sc.forbid_tools:
        c.check(f"工具 {t} **未被执行**", t not in r.tools_called, str(r.tools_called))
    for t in sc.expect_refused:
        c.check(f"★ 工具 {t} 被**闸门拒绝**", t in r.refused, str(r.refused))

    if sc.min_loop_rounds is not None:
        c.check(f"工具循环轮数 ≥ {sc.min_loop_rounds}",
                r.tool_loop_rounds >= sc.min_loop_rounds, str(r.tool_loop_rounds))
    if sc.max_loop_rounds is not None:
        c.check(f"工具循环轮数 ≤ {sc.max_loop_rounds}（跳过了）",
                r.tool_loop_rounds <= sc.max_loop_rounds, str(r.tool_loop_rounds))
    if sc.expect_kb_calls is not None:
        c.check(f"知识库检索次数 = {sc.expect_kb_calls}",
                r.kb_calls == sc.expect_kb_calls, str(r.kb_calls))
    if sc.expect_l4_calls is not None:
        c.check(f"L4 记忆召回次数 = {sc.expect_l4_calls}",
                r.l4_calls == sc.expect_l4_calls, str(r.l4_calls))
    if sc.expect_crystallized_empty:
        c.check("★ 被拒轨迹**不进结晶档**（G2 门禁生效）",
                r.crystallized_added == [], str(r.crystallized_added))
    if sc.expect_fed_back:
        c.check(f"★ {sc.expect_fed_back} 的结果**回灌进消息**",
                r.fed_back(sc.expect_fed_back), f"messages={len(r.last_messages)}")
    if sc.max_system_chars is not None:
        sysmsg = r.last_messages[0]["content"] if r.last_messages else ""
        c.check(f"★ system prompt 被截短到 ≤{sc.max_system_chars} 字",
                len(sysmsg) <= sc.max_system_chars, f"实际 {len(sysmsg)}")

    c.check("答案非空（链路走完）", bool(r.answer.strip()), repr(r.answer[:40]))


def main() -> int:
    c = Checker()
    print("=" * 78)
    print("产物级场景评测（脚本化 LLM · 零 API）")
    print("=" * 78)
    for i, sc in enumerate(SCENARIOS, 1):
        print()
        print(f"[{i}] {sc.name}")
        print("-" * 78)
        _run_one(sc, c, i)

    print()
    print("=" * 78)
    print(f"场景评测: {c.passed}/{c.total} 断言通过 · {len(SCENARIOS)} 个场景")
    print(f"{'达标 ✓' if c.passed == c.total else '存在失败项 ✗'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())