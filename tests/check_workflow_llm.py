# -*- coding: utf-8 -*-
"""LLM 节点与人工确认守卫 —— 输出契约 + 熔断 + 续跑。

【定位】`docs/ORCHESTRATION.md` §6.1 的 P2 补充守卫（第 6 个）。

【零 API 成本】`providers.default_client` 被替换成**假客户端**，
        所以本守卫不花一分钱，也不依赖网络。

【最关键的几条】
        · **输出契约 Fail-Closed**：模型少给一个字段 → 节点失败，而不是给下游 `None`；
        · **人工确认是暂停而不是完成**：`run.state == waiting`，下游不被调度；
        · **熔断在第 N 次打回触发**：连续打回到 `max_reject` → 终态 `blocked`，
          **不回退、不重试**；
        · **打回计数随断点落盘** —— 否则重启后"连续 3 次"这个约束就失效了。

【用法】python tests/check_workflow_llm.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import providers                                     # noqa: E402
from core.orchestration import engine as E                     # noqa: E402
from core.orchestration import loader, store                   # noqa: E402
from core.orchestration.model import StepState                 # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "_orch_llm_sandbox"
PROMPT = ROOT / "prompts" / "orch_guard_test.md"


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


class FakeClient:
    """假模型客户端。`reply` 可设为任意字符串，用来模拟契约违背。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def generate_response(self, messages):                    # noqa: ANN001
        self.calls += 1
        return self.reply


class patch_client:
    def __init__(self, reply: str) -> None:
        self.fake = FakeClient(reply)

    def __enter__(self):
        self._orig = providers.default_client
        providers.default_client = lambda **kw: self.fake    # type: ignore[assignment]
        return self.fake

    def __exit__(self, *exc):
        providers.default_client = self._orig                 # type: ignore[assignment]
        return False


class patch_store_dir:
    def __enter__(self):
        self._orig = store.runs_dir
        store.runs_dir = lambda: (SANDBOX.mkdir(parents=True, exist_ok=True) or SANDBOX)  # type: ignore[assignment]
        return SANDBOX

    def __exit__(self, *exc):
        store.runs_dir = self._orig                            # type: ignore[assignment]
        return False


LLM_WF = """
id: llm_guard
nodes:
  - { id: think, type: llm.decide, params: { prompt_ref: prompts/orch_guard_test.md, output_schema: { a: { type: string }, b: { type: number } } }, side_effect: network }
"""


def main() -> int:
    c = Checker()
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    PROMPT.parent.mkdir(parents=True, exist_ok=True)
    PROMPT.write_text("测试用提示词，请输出 JSON。", encoding="utf-8")

    wf_llm = loader.load_text(LLM_WF)

    # ------------------------------------------------------------------
    c.section("第 1 节：★★ 输出契约 Fail-Closed")
    # ------------------------------------------------------------------
    cases = [
        ("合法 JSON 且字段齐全", '{"a": "x", "b": 1}', True),
        ("带 ```json 围栏（应被容忍）", '```json\n{"a": "x", "b": 1}\n```', True),
        ("★ 缺字段 b", '{"a": "x"}', False),
        ("★ 不是 JSON", "我觉得四月上旬比较合适", False),
        ("★ 是 JSON 但不是对象", '["a", "b"]', False),
        ("★ 空回复", "", False),
    ]
    for name, reply, want_ok in cases:
        with patch_client(reply) as fake:
            run = E.Engine(wf_llm, persist=False).run()
        st = run.steps["think"]
        c.check(f"{name} → {'成功' if want_ok else '失败'}",
                (st.state == StepState.DONE) is want_ok,
                f"state={st.state.value} err={st.error[:70]}")
        if not want_ok:
            c.check(f"   {name}：错误信息**说清原因**（不是空）", bool(st.error))

    # ------------------------------------------------------------------
    c.section("第 2 节：★ 契约缺字段时错误信息要点名是哪个字段")
    # ------------------------------------------------------------------
    with patch_client('{"a": "x"}'):
        run = E.Engine(wf_llm, persist=False).run()
    c.check("★ 错误里出现缺失字段名 'b'",
            "b" in run.steps["think"].error, run.steps["think"].error[:80])

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 无契约 / 无提示词 → 直接拒绝（不调模型）")
    # ------------------------------------------------------------------
    wf_noschema = loader.load_text("""
id: noschema
nodes:
  - { id: t, type: llm.decide, params: { prompt_ref: prompts/orch_guard_test.md }, side_effect: network }
""")
    with patch_client('{"a": 1}') as fake:
        run = E.Engine(wf_noschema, persist=False).run()
    c.check("★ 没有 output_schema → 节点失败", run.steps["t"].state == StepState.FAILED)
    c.check("★★ 且**一次模型调用都没发生**（省钱且快速失败）", fake.calls == 0,
            f"实际调用 {fake.calls} 次")

    wf_noprompt = loader.load_text("""
id: noprompt
nodes:
  - { id: t, type: llm.decide, params: { prompt_ref: prompts/__不存在__.md, output_schema: { a: { type: string } } }, side_effect: network }
""")
    with patch_client('{"a": 1}') as fake2:
        run = E.Engine(wf_noprompt, persist=False).run()
    c.check("★ 提示词文件不存在 → 节点失败", run.steps["t"].state == StepState.FAILED)
    c.check("★ 且同样**不调模型**", fake2.calls == 0, f"实际 {fake2.calls} 次")

    # ------------------------------------------------------------------
    c.section("第 4 节：★★ 人工确认 —— 暂停、不调度下游")
    # ------------------------------------------------------------------
    with patch_store_dir():
        wf_h = loader.load_text("""
id: human_guard
nodes:
  - { id: prep, type: data.kb_query, params: { query: q }, side_effect: none }
  - { id: sign, type: human.confirm, params: { prompt: 确认？, max_reject: 2 }, side_effect: none, needs: [prep] }
  - { id: after, type: data.kb_query, params: { query: q }, side_effect: none, needs: [sign] }
""")
        eng = E.Engine(wf_h, persist=True)
        rid = "hguard-001"
        run = eng.run(run_id=rid, inputs={"__kb__": _StubKB()})
        c.check("★★ 人工确认节点 → run.state = **waiting**（不是 done/failed）",
                run.state == StepState.WAITING, run.state.value)
        c.check("★ sign 节点状态 = waiting", run.steps["sign"].state == StepState.WAITING)
        c.check("★★ 下游 after **从未被调度**（停在 pending）",
                run.steps["after"].state == StepState.PENDING)

        cp = store.load_checkpoint(rid)
        c.check("★ 断点里记下了 waiting_node", cp is not None and cp.waiting_node == "sign",
                cp.waiting_node if cp else "None")

        # ---- 打回 1 次（未到上限 2）----
        run = eng.decide(rid, approved=False)
        c.check("★ 打回 1 次（未到上限）→ 仍 waiting", run.state == StepState.WAITING,
                run.state.value)
        c.check("★ 打回计数被记为 1", run.steps["sign"].rejects == 1,
                str(run.steps["sign"].rejects))
        cp2 = store.load_checkpoint(rid)
        c.check("★★ 打回计数**随断点落盘**（重启后约束仍然有效）",
                cp2 is not None and store.load_run(rid).steps["sign"].rejects == 1)

        # ---- 打回第 2 次 → 熔断 ----
        run = eng.decide(rid, approved=False)
        c.check("★★ 达到 max_reject=2 → 终态 **blocked**",
                run.state == StepState.BLOCKED, run.state.value)
        c.check("★ sign 节点状态 = blocked", run.steps["sign"].state == StepState.BLOCKED)
        c.check("★ 错误信息说明熔断原因", "熔断" in run.steps["sign"].error,
                run.steps["sign"].error[:60])
        c.check("★★ 熔断后下游 after **仍未被调度**", run.steps["after"].state == StepState.PENDING)

        # ---- 另一个 run：批准 ----
        rid2 = "hguard-002"
        eng2 = E.Engine(wf_h, persist=True)
        r2 = eng2.run(run_id=rid2, inputs={"__kb__": _StubKB()})
        c.check("新 run 同样先 waiting", r2.state == StepState.WAITING)
        r2 = eng2.decide(rid2, approved=True, inputs={"__kb__": _StubKB()})
        c.check("★★ 批准后整体 **done**", r2.state == StepState.DONE, r2.state.value)
        c.check("★ sign 节点 done", r2.steps["sign"].state == StepState.DONE)
        c.check("★ 下游 after 也 done（续跑链通了）",
                r2.steps["after"].state == StepState.DONE, r2.steps["after"].state.value)

        # ---- 对不存在的 run 做 decide → 明确报错，不是静默 ----
        try:
            eng.decide("no-such-run", approved=True)
            c.check("★ decide 一个不存在的 run 应报错", False, "居然没抛异常")
        except E.EngineError as e:
            c.check("★ decide 不存在的 run → 抛出 EngineError", True)
            c.check("★ 错误信息说明原因", "断点" in str(e), str(e)[:60])

    if PROMPT.exists():
        PROMPT.unlink()
    shutil.rmtree(SANDBOX, ignore_errors=True)

    print()
    print("=" * 78)
    print(f"LLM 节点与人工确认守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


class _StubKB:
    """方法名与 `KnowledgeBase.retrieve_context()` 保持一致（替身不许用真类没有的名字）。"""

    def retrieve_context(self, query, k=3, **kwargs):         # noqa: ANN001
        return f"stub:{query}"


if __name__ == "__main__":
    raise SystemExit(main())