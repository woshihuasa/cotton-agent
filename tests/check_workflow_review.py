# -*- coding: utf-8 -*-
"""AI 审查门禁守卫（`gate.agent_review`）—— **Fail-Closed** + 不得冒充人类终审。

【定位】P5 的 ① 半边（`docs/ORCHESTRATION.md`：审查员 → `gate.agent_review` 节点）。

【为什么单独守它】
        它同时踩两条本项目的红线：
          ① **它是"会花钱的门禁"** —— 每跑一次调一次模型，写错了就白烧钱；
          ② **它长得像"人工验收"** —— `docs/ABSORPTION.md` §5.3 明确记过
             「已完成→已验收」**仅人类专属，严禁 Agent 代签**。
             所以本守卫要断言：它的产出里**必须**写明"不构成人类终审"。

【零 API 成本】`providers.default_client` 被替换成假客户端。

【用法】python tests/check_workflow_review.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import providers                                   # noqa: E402
from core.orchestration import catalog, engine as E, loader    # noqa: E402
from core.orchestration.model import StepState               # noqa: E402


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


class patch_reply:
    """同一个 fake 对象，但可中途改 reply（模拟"先说通过、再说拒绝"）。"""

    def __init__(self) -> None:
        self.fake = FakeClient("{}")

    def __enter__(self):
        self._orig = providers.default_client
        providers.default_client = lambda **kw: self.fake    # type: ignore[assignment]
        return self.fake

    def __exit__(self, *exc):
        providers.default_client = self._orig                 # type: ignore[assignment]
        return False


def _wf(params_yaml: str, *, after: bool = False) -> "E.Engine":
    tail = ("""
  - { id: after, type: data.kb_query, params: { query: q }, side_effect: none, needs: [review] }
""" if after else "")
    text = f"""
id: review_guard
nodes:
  - {{ id: src, type: data.kb_query, params: {{ query: q }}, side_effect: none }}
  - id: review
    type: gate.agent_review
    side_effect: network
    needs: [src]
    params:
{params_yaml}
{tail}
"""
    return E.Engine(loader.load_text(text), persist=False)


BASE = """      rubric: 只要非空即合格
      inputs: [src]
      threshold: 0.7"""


class _StubKB:
    """方法名与 `KnowledgeBase.retrieve_context()` 保持一致（替身不许用真类没有的名字）。"""

    def retrieve_context(self, query, k=3, **kwargs):         # noqa: ANN001
        return f"stub:{query}"


def run_with(reply: str, params_yaml: str = BASE, *, after: bool = False):
    eng = _wf(params_yaml, after=after)
    with patch_client(reply) as fake:
        run = eng.run(inputs={"__kb__": _StubKB()})
    return run, fake


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：目录与执行器注册")
    # ------------------------------------------------------------------
    spec = catalog.get("gate.agent_review")
    c.check("★ `gate.agent_review` 在目录里", spec is not None)
    c.check("★ 在 EXECUTABLE_TYPES 里", "gate.agent_review" in catalog.EXECUTABLE_TYPES)
    c.check("★ 副作用是 network（会调模型、会花钱）",
            spec is not None and spec.side_effect.value == "network",
            spec.side_effect.value if spec else "None")
    c.check("★ 命中专用执行器（**不是**确定性门禁的那条路）",
            E.executor_for("gate.agent_review") is E._exec_gate_review)
    c.check("确定性门禁仍走 `_exec_gate`", E.executor_for("gate.range") is E._exec_gate)
    c.check("契约里 threshold 有默认值",
            any(p.name == "threshold" and p.default is not None for p in spec.params))

    # ------------------------------------------------------------------
    c.section("第 2 节：★★ 通过的路径 —— 且**必须**声明不是人类终审")
    # ------------------------------------------------------------------
    run, fake = run_with('{"passed": true, "score": 0.9, "reason": "合格"}')
    c.check("★ 合格 → 节点 done", run.steps["review"].state == StepState.DONE,
            run.steps["review"].state.value)
    out = run.steps["review"].output or {}
    c.check("★ 产出里带 score / reason",
            out.get("score") == 0.9 and out.get("reason") == "合格", str(out)[:80])
    c.check("★★★ **产出明确写着「不构成人类终审」**（严禁 Agent 代签）",
            "不构成人类终审" in str(out.get("note", "")), str(out.get("note"))[:80])
    c.check("★★ 且提示要用 human.confirm 才算签字",
            "human.confirm" in str(out.get("note", "")))
    c.check("调用了 1 次模型", fake.calls == 1, str(fake.calls))

    # ------------------------------------------------------------------
    c.section("第 3 节：★★ 拒绝的路径（Fail-Closed）")
    # ------------------------------------------------------------------
    cases = [
        ("模型判不合格", '{"passed": false, "score": 0.2, "reason": "缺单位"}'),
        ("★ 说通过但分数低于阈值（不采信自相矛盾）",
         '{"passed": true, "score": 0.3, "reason": "还行"}'),
        ("★ 不是 JSON", "我觉得挺好的"),
        ("★ JSON 但缺字段", '{"passed": true}'),
        ("★ score 不是数字", '{"passed": true, "score": "高", "reason": "x"}'),
        ("★ 空回复", ""),
        ("★ 是 JSON 但不是对象", '["passed"]'),
    ]
    for name, reply in cases:
        run, _ = run_with(reply)
        st = run.steps["review"]
        c.check(f"{name} → **节点 failed**", st.state == StepState.FAILED, st.state.value)
        c.check(f"   {name} → 错误信息非空", bool(st.error))

    # 阈值边界：score == threshold 应通过（闭区间）
    run, _ = run_with('{"passed": true, "score": 0.7, "reason": "刚好"}')
    c.check("★ score 恰好等于阈值 → 通过（闭区间）",
            run.steps["review"].state == StepState.DONE, run.steps["review"].state.value)

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 配置错误必须在**调模型之前**就拒（省钱）")
    # ------------------------------------------------------------------
    no_rubric = """      inputs: [src]
      threshold: 0.7"""
    run, fake = run_with('{"passed": true, "score": 0.9, "reason": "x"}', no_rubric)
    c.check("★ 既无 rubric 也无 rubric_ref → failed", run.steps["review"].state == StepState.FAILED)
    c.check("★★ 且**一次模型调用都没发生**", fake.calls == 0, f"实际 {fake.calls} 次")

    bad_file = """      rubric_ref: prompts/__不存在__.md
      inputs: [src]"""
    run, fake = run_with('{"passed": true, "score": 0.9, "reason": "x"}', bad_file)
    c.check("★ rubric_ref 文件不存在 → failed", run.steps["review"].state == StepState.FAILED)
    c.check("★ 且不调模型", fake.calls == 0, f"实际 {fake.calls} 次")

    no_inputs = """      rubric: x
      threshold: 0.7"""
    run, fake = run_with('{"passed": true, "score": 0.9, "reason": "x"}', no_inputs)
    c.check("★ 没有 inputs → failed", run.steps["review"].state == StepState.FAILED)
    c.check("★ 且不调模型", fake.calls == 0, f"实际 {fake.calls} 次")

    bad_thr = """      rubric: x
      inputs: [src]
      threshold: 2.5"""
    run, fake = run_with('{"passed": true, "score": 0.9, "reason": "x"}', bad_thr)
    c.check("★ threshold 超出 0-1 → failed", run.steps["review"].state == StepState.FAILED)
    c.check("★ 且不调模型", fake.calls == 0, f"实际 {fake.calls} 次")

    ghost = """      rubric: x
      inputs: [不存在的节点]"""
    run, fake = run_with('{"passed": true, "score": 0.9, "reason": "x"}', ghost)
    c.check("★ inputs 指向不存在的节点 → failed", run.steps["review"].state == StepState.FAILED)
    c.check("★ 且不调模型", fake.calls == 0, f"实际 {fake.calls} 次")

    # ------------------------------------------------------------------
    c.section("第 5 节：★ 审查拦住时，下游必须不被调度")
    # ------------------------------------------------------------------
    run, _ = run_with('{"passed": false, "score": 0.1, "reason": "不合格"}', after=True)
    c.check("★★ 审查未通过 → 整体 failed", run.state == StepState.FAILED, run.state.value)
    c.check("★★ 下游 after **停在 pending**（从未被调度）",
            run.steps["after"].state == StepState.PENDING, run.steps["after"].state.value)

    # ------------------------------------------------------------------
    c.section("第 6 节：★ 它是「第一道过滤」——文档口径一致")
    # ------------------------------------------------------------------
    desc = (spec.description if spec else "") or ""
    c.check("★ 契约描述写明是「第一道过滤，非人类终审」",
            "第一道过滤" in desc and "非人类终审" in desc, desc[:80])
    src = (ROOT / "core" / "orchestration" / "engine.py").read_text(encoding="utf-8")
    c.check("★ 执行器源码里保留「严禁 Agent 代签」的依据引用",
            "严禁 Agent 代签" in src)

    print()
    print("=" * 78)
    print(f"AI 审查门禁守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
