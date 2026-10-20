# -*- coding: utf-8 -*-
"""执行器守卫 —— 批次划分 / 重试纪律 / 终态单调性 / 失败策略。

【定位】`docs/ORCHESTRATION.md` §6.1 的第 3 个守卫（P1）。

【最关键的几条】
        · **仅幂等节点可重试** —— 重复写盘比"少重试一次"危险得多；
        · **可选节点失败不该拖垮整轮** —— 这正是第一版实现里被本守卫抓到的 bug；
        · **终态单调性** —— 熔断/失败之后不许被重新拉起来跑。

【零 LLM / 零 API】节点执行函数用**注入的假执行器**，不碰真实工具。

【用法】python tests/check_workflow_engine.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.orchestration import engine as E                     # noqa: E402
from core.orchestration import loader                          # noqa: E402
from core.orchestration.model import (                         # noqa: E402
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
        print(f"  [{'OK  ' if ok else '✗ FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


class FakeNode:
    """可控的假执行器：记录调用次数、可设失败次数、可设耗时。"""

    def __init__(self, *, fail_times: int = 0, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self.fail_times = fail_times
        self.delay = delay
        self._failed = 0
        self._lock = __import__("threading").Lock()

    def __call__(self, node, ctx):                            # noqa: ANN001
        with self._lock:
            self.calls.append(node.id)
        if self.delay:
            time.sleep(self.delay)
        if self._failed < self.fail_times:
            self._failed += 1
            return E.NodeOutcome(ok=False, error=f"模拟失败 #{self._failed}")
        return E.NodeOutcome(ok=True, output={"v": 1, "id": node.id})


class patch_executor:
    """把执行器临时换成假的（`executor_for` 是模块级函数，按需打补丁）。"""

    def __init__(self, fake) -> None:                         # noqa: ANN001
        self.fake = fake
        self._orig = None

    def __enter__(self):
        self._orig = E.executor_for
        E.executor_for = lambda node_type: self.fake          # type: ignore[assignment]
        return self.fake

    def __exit__(self, *exc):
        E.executor_for = self._orig                           # type: ignore[assignment]
        return False


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：★ 批次划分（同批 = 可并发）")
    # ------------------------------------------------------------------
    wf = loader.load_text("""
id: batches
nodes:
  - { id: a, type: data.kb_query, params: { query: q }, side_effect: none }
  - { id: b, type: data.kb_query, params: { query: q }, side_effect: none }
  - { id: c1, type: data.kb_query, params: { query: q }, side_effect: none, needs: [a] }
  - { id: c2, type: data.kb_query, params: { query: q }, side_effect: none, needs: [b] }
  - { id: d, type: data.kb_query, params: { query: q }, side_effect: none, needs: [c1, c2] }
""")
    fake = FakeNode()
    with patch_executor(fake):
        ev: list[dict] = []
        run = E.Engine(wf, persist=False, on_event=ev.append).run(
            inputs={"__kb__": _StubKB()})
    starts = [e["node"] for e in ev if e["event"] == "node_start"]
    c.check("★ 第一批 = {a, b}（都无依赖，同批并发）",
            set(starts[:2]) == {"a", "b"}, str(starts))
    c.check("★ 第二批 = {c1, c2}", set(starts[2:4]) == {"c1", "c2"}, str(starts))
    c.check("★ 最后才是 d（依赖 c1、c2 都完成）", starts[-1] == "d", str(starts))
    c.check("整体 done", run.state == StepState.DONE, run.state.value)

    # ------------------------------------------------------------------
    c.section("第 2 节：★★ 重试纪律 —— 仅幂等节点可重试")
    # ------------------------------------------------------------------
    # 幂等（read_only → none）：应重试 3 次
    wf_idem = loader.load_text("""
id: idem
nodes:
  - { id: x, type: data.kb_query, params: { query: q }, side_effect: none, retry: 2 }
""")
    fake2 = FakeNode(fail_times=99)          # 永远失败
    with patch_executor(fake2):
        run2 = E.Engine(wf_idem, persist=False).run(inputs={"__kb__": _StubKB()})
    c.check("★ 幂等节点 retry=2 → 一共尝试 **3 次**",
            len(fake2.calls) == 3, f"实际 {len(fake2.calls)} 次")
    c.check("幂等节点重试完仍失败 → failed", run2.state == StepState.FAILED)

    # 非幂等（disk）：即使写 retry 也不许重试
    wf_disk = loader.load_text("""
id: disk
nodes:
  - { id: y, type: tool.plot_trend, params: { metric: yield }, side_effect: disk, retry: 5 }
""")
    fake3 = FakeNode(fail_times=99)
    with patch_executor(fake3):
        E.Engine(wf_disk, persist=False).run()
    c.check("★★ **非幂等节点（disk）写 retry=5 → 仍然只尝试 1 次**",
            len(fake3.calls) == 1, f"实际 {len(fake3.calls)} 次")

    # 未声明副作用 → 保守按 disk → 也不重试
    wf_undecl = loader.load_text("""
id: undecl
nodes:
  - { id: z, type: data.kb_query, params: { query: q }, retry: 3 }
""")
    fake4 = FakeNode(fail_times=99)
    with patch_executor(fake4):
        E.Engine(wf_undecl, persist=False).run(inputs={"__kb__": _StubKB()})
    c.check("★ 未声明副作用 → 保守按 disk → **只尝试 1 次**",
            len(fake4.calls) == 1, f"实际 {len(fake4.calls)} 次")

    # ------------------------------------------------------------------
    c.section("第 3 节：★★ 可选节点失败**不该**拖垮整轮（第一版真 bug）")
    # ------------------------------------------------------------------
    wf_opt = loader.load_text("""
id: opt
nodes:
  - { id: bad, type: data.kb_query, params: { query: q }, side_effect: none, optional: true, on_fail: skip_if_optional }
  - { id: ok, type: data.kb_query, params: { query: q }, side_effect: none }
""")
    fake5 = FakeNode(fail_times=1)           # 只让第一个失败的节点失败一次
    with patch_executor(fake5):
        run5 = E.Engine(wf_opt, persist=False).run(inputs={"__kb__": _StubKB()})
    c.check("★ 可选节点 failed、整体仍 **done**",
            run5.state == StepState.DONE, run5.state.value)

    # 必选节点失败 → 整体 failed，且下游不被调度
    wf_req = loader.load_text("""
id: req
nodes:
  - { id: r, type: data.kb_query, params: { query: q }, side_effect: none }
  - { id: after, type: data.kb_query, params: { query: q }, side_effect: none, needs: [r] }
""")
    fake6 = FakeNode(fail_times=99)
    with patch_executor(fake6):
        run6 = E.Engine(wf_req, persist=False).run(inputs={"__kb__": _StubKB()})
    c.check("★ 必选节点失败 → 整体 failed", run6.state == StepState.FAILED)
    c.check("★ 且下游节点**从未被调度**（停在 pending）",
            run6.steps["after"].state == StepState.PENDING,
            run6.steps["after"].state.value)

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 超时按失败处理（不假装成功）")
    # ------------------------------------------------------------------
    wf_slow = loader.load_text("""
id: slow
nodes:
  - { id: s, type: data.kb_query, params: { query: q }, side_effect: none, timeout_s: 0.05 }
""")
    fake7 = FakeNode(delay=0.4)
    with patch_executor(fake7):
        run7 = E.Engine(wf_slow, persist=False).run(inputs={"__kb__": _StubKB()})
    c.check("★ 超时 → 节点 failed", run7.steps["s"].state == StepState.FAILED)
    c.check("★ 错误信息里写明超时", "超时" in run7.steps["s"].error,
            run7.steps["s"].error[:60])

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ 终态单调性（熔断/失败之后不许再跑）")
    # ------------------------------------------------------------------
    for src in (StepState.DONE, StepState.BLOCKED, StepState.FAILED):
        c.check(f"★ {src.value} 是终态：不许迁回 running",
                _bad_transition(src, StepState.RUNNING))
    c.check("pending → running → done 这条正常路径是通的",
            _ok_transition(StepState.PENDING, StepState.RUNNING)
            and _ok_transition(StepState.RUNNING, StepState.DONE))
    c.check("★ waiting → resumed 合法（人工放行）",
            _ok_transition(StepState.WAITING, StepState.RESUMED))

    # ------------------------------------------------------------------
    c.section("第 6 节：P2 边界 —— llm.* 已实现，缺提示词要报得清楚")
    # ------------------------------------------------------------------
    # ⚠️ 这里原来是"断言 llm.* 报错说要延到 P2" —— P2 实现后该断言**过期**了
    #     （守卫抓到的：它测的是阶段边界，而阶段已经推进）。改为测真实行为。
    wf_llm = loader.load_text("""
id: llmcase
nodes:
  - { id: think, type: llm.decide, params: { prompt_ref: prompts/__不存在的文件__.md, output_schema: { a: { type: string } } }, side_effect: network }
""")
    run8 = E.Engine(wf_llm, persist=False).run()
    c.check("★ 缺提示词文件 → 节点 failed", run8.state == StepState.FAILED)
    c.check("★ 错误信息**点名是提示词文件的问题**（不是含糊的「未知错误」）",
            "提示词文件不存在" in run8.steps["think"].error,
            run8.steps["think"].error[:80])
    c.check("`executor_for('llm.decide')` 有真实执行器（不是占位）",
            E.executor_for("llm.decide") is E._exec_llm)
    c.check("`executor_for('human.confirm')` 有真实执行器",
            E.executor_for("human.confirm") is E._exec_human_confirm)

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"执行器守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


class _StubKB:
    """`data.kb_query` 需要注入的知识库替身。"""

    def search(self, q, top_k=3):                            # noqa: ANN001
        return [{"text": f"stub:{q}", "score": 0.9}]


def _ok_transition(src: StepState, dst: StepState) -> bool:
    try:
        assert_transition(src, dst)
        return True
    except TransitionError:
        return False


def _bad_transition(src: StepState, dst: StepState) -> bool:
    return not _ok_transition(src, dst)


if __name__ == "__main__":
    raise SystemExit(main())