# -*- coding: utf-8 -*-
"""工作流**静态校验**守卫 —— 每类错误各造一个反例，**必须被拒**。

【定位】`docs/ORCHESTRATION.md` §6.1 的第 2 个守卫（P0）。

【为什么这样写】"校验器能通过合法工作流"没有意义 —— 一个永远返回 OK 的校验器也能通过。
        **真正要证明的是"它抓得住错误"**，所以本守卫的主力是**反例**：
        每一类错误造一个最小工作流，断言它**必须**产生对应的 code。

        这与本项目一直在做的事一致：**反向断言比正向断言更有价值**。

【零 LLM / 零 API】纯静态，连文件都不用写。

【用法】python tests/check_workflow_validate.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.orchestration import loader, validator             # noqa: E402
from core.orchestration.model import (                       # noqa: E402
    StepState,
    TransitionError,
    assert_transition,
)


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


#: 一个**最小的合法工作流**。所有反例都在它的基础上改一处，保证"只有一个错"。
BASELINE = """
id: demo
version: 1
inputs:
  region: { type: string, required: true }
nodes:
  - id: a
    type: data.kb_query
    params: { query: "{{region}}" }
    side_effect: none
  - id: b
    type: tool.query_cotton_stats
    params: { region: "{{region}}", year: 2021, metric: yield, stat: value }
    side_effect: none
    needs: [a]
"""


def codes(issues: list[validator.Issue]) -> set[str]:
    return {i.code for i in issues}


def check_rejected(c: Checker, name: str, yaml_text: str, want_code: str) -> None:
    """断言：这份 YAML **必须**产生 `want_code` 这个错误。"""
    try:
        wf, issues = loader.load_and_check(yaml_text)
    except loader.LoaderError as e:
        # 加载阶段就被拒也行 —— 只要"被拒了"
        c.check(f"{name} → 被拒（加载期）", True, str(e)[:70])
        return
    got = codes(issues)
    c.check(f"{name} → **被拒**（期望 code={want_code}）",
            want_code in got, f"实际: {sorted(got) or '无问题'}")


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：基准工作流**必须通过**（否则后面全是假阳性）")
    # ------------------------------------------------------------------
    wf, issues = loader.load_and_check(BASELINE)
    c.check("基准能加载", wf.id == "demo" and len(wf.nodes) == 2, str(wf.ids()))
    c.check("★ 基准校验**无错误**", validator.is_valid(issues),
            validator.format_issues(validator.errors_only(issues)))
    c.check("基准的边正确（a → b）", wf.edges() == [("a", "b")], str(wf.edges()))

    # ------------------------------------------------------------------
    c.section("第 2 节：★★ 每一类错误都要**抓得住**（反例集）")
    # ------------------------------------------------------------------
    # ① 未知节点类型（拼错）
    check_rejected(c, "① 拼错工具名 tool.get_wether", BASELINE.replace(
        "type: tool.query_cotton_stats", "type: tool.get_wether"), "unknown-node-type")

    # ② 已设计但未实现的类型
    check_rejected(c, "② 用了未实现的 skill.match", BASELINE.replace(
        "type: tool.query_cotton_stats", "type: skill.match"), "unknown-node-type")

    # ③ 重复 id
    check_rejected(c, "③ 两个节点同名", BASELINE.replace(
        "- id: b", "- id: a"), "duplicate-id")

    # ④ 悬空依赖
    check_rejected(c, "④ needs 指向不存在的节点", BASELINE.replace(
        "needs: [a]", "needs: [ghost]"), "dangling-need")

    # ⑤ 自依赖
    check_rejected(c, "⑤ 节点依赖自己", BASELINE.replace(
        "needs: [a]", "needs: [b]"), "self-dependency")

    # ⑥ 环（a→b, b→a）
    cycle = """
id: cyc
nodes:
  - { id: a, type: gate.range, params: { value: x }, side_effect: none, needs: [b] }
  - { id: b, type: gate.range, params: { value: y }, side_effect: none, needs: [a] }
"""
    check_rejected(c, "⑥ 两个节点互相依赖（环）", cycle, "cycle")

    # ⑦ 必填参数缺失
    check_rejected(c, "⑦ tool.get_weather 缺必填 city", """
id: missing
nodes:
  - { id: w, type: tool.get_weather, params: {}, side_effect: network }
""", "missing-params")

    # ⑧ 未知参数名（拼错参数）
    check_rejected(c, "⑧ 工具参数拼错 city→ctiy", """
id: typo_param
nodes:
  - { id: w, type: tool.get_weather, params: { ctiy: 阿克苏 }, side_effect: network }
""", "unknown-params")

    # ⑨ 顶层字段拼错（静默忽略是更糟的行为）
    c.check("⑨ 顶层字段拼错 → 加载期就拒（不静默忽略）",
            _rejects_at_load(BASELINE + "\nextra_field: 1"), "")

    # ⑩ 节点字段拼错
    c.check("⑩ 节点字段拼错 → 加载期就拒", _rejects_at_load(
        BASELINE.replace("needs: [a]", "nedes: [a]")), "")

    # ------------------------------------------------------------------
    c.section("第 3 节：WARN 类不该阻断加载（但要报出来）")
    # ------------------------------------------------------------------
    # 未声明副作用 → WARN，合法
    warn_doc = """
id: warn_case
nodes:
  - { id: a, type: data.kb_query, params: { query: q } }
"""
    wf2, iss2 = loader.load_and_check(warn_doc)
    c.check("未声明 side_effect → **WARN 而非 ERROR**", validator.is_valid(iss2),
            validator.format_issues(iss2))
    c.check("★ 但**必须报出来**（不能静默）",
            "side-effect-undeclared" in codes(iss2), str(sorted(codes(iss2))))

    # 非幂等却写 retry → WARN
    retry_doc = """
id: retry_case
nodes:
  - { id: p, type: tool.plot_trend, params: { metric: yield }, side_effect: disk, retry: 3 }
"""
    wf3, iss3 = loader.load_and_check(retry_doc)
    c.check("★ 写盘节点写 retry=3 → 报 retry-ignored（会被取 0，别让人误会）",
            "retry-ignored" in codes(iss3), str(sorted(codes(iss3))))
    c.check("★ 且该节点的生效重试确实是 0", wf3.nodes[0].effective_retry == 0,
            str(wf3.nodes[0].effective_retry))

    # 孤立节点 → WARN
    orphan = """
id: orphan_case
nodes:
  - { id: a, type: data.kb_query, params: { query: q }, side_effect: none }
  - { id: b, type: tool.query_cotton_stats, params: { region: x, year: 2021 }, side_effect: none }
"""
    _wf4, iss4 = loader.load_and_check(orphan)
    c.check("孤立节点 → WARN", "orphan-node" in codes(iss4), str(sorted(codes(iss4))))

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 状态机 —— 终态不可出（这是熔断的语义基础）")
    # ------------------------------------------------------------------
    c.check("pending → running 合法",
            _ok_transition(StepState.PENDING, StepState.RUNNING))
    c.check("running → done 合法",
            _ok_transition(StepState.RUNNING, StepState.DONE))
    c.check("waiting → resumed 合法（人工放行）",
            _ok_transition(StepState.WAITING, StepState.RESUMED))
    for src in (StepState.DONE, StepState.BLOCKED, StepState.FAILED):
        for dst in StepState:
            if dst == src:
                continue
            c.check(f"★★ **{src.value} 是终态，不许迁到 {dst.value}**",
                    not _ok_transition(src, dst))
    c.check("★ 不许跳过 running 直接 pending→done",
            not _ok_transition(StepState.PENDING, StepState.DONE))

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"工作流静态校验守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


def _rejects_at_load(yaml_text: str) -> bool:
    try:
        loader.load_text(yaml_text)
    except loader.LoaderError:
        return True
    return False


def _ok_transition(src: StepState, dst: StepState) -> bool:
    try:
        assert_transition(src, dst)
        return True
    except TransitionError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())