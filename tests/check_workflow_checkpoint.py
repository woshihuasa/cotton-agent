# -*- coding: utf-8 -*-
"""断点续跑守卫 —— 落盘 → 重载 → 续跑，且**已完成节点不重复执行**。

【定位】`docs/ORCHESTRATION.md` §6.1 的第 4 个守卫（P1）。

【最关键的一条】续跑的正确性**不在于"知道跑到哪了"**，而在于
        **"已完成节点的产出还在"** —— 否则下游拿到 `None`，
        表现为"莫名其妙的数据错误"，而问题在很远的地方爆发。
        （这与 `04` 号 §8.7 那个 L4 静默失效是同一类风险。）

【隔离】本守卫把 `store.runs_dir` **临时重定向到 `tests/eval_outputs/` 下的沙箱**，
        不碰真实的 `workflows/runs/`。

【零 LLM / 零 API】

【用法】python tests/check_workflow_checkpoint.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.orchestration import engine as E                     # noqa: E402
from core.orchestration import loader, store                   # noqa: E402
from core.orchestration.model import Checkpoint, StepState     # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "_orch_ckpt_sandbox"


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


class FakeNode:
    """记录每次调用 —— 用来断言"续跑时没有重新执行"。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, node, ctx):                            # noqa: ANN001
        self.calls.append(node.id)
        # 引用上游产出，验证断点恢复后下游能拿到真实值
        upstream = ctx.outputs.get("s1")
        return E.NodeOutcome(ok=True, output={
            "id": node.id, "v": 42, "saw_upstream": upstream is not None,
        })


class patch_store_dir:
    """把 `store.runs_dir` 临时指向沙箱。"""

    def __enter__(self):
        self._orig = store.runs_dir
        store.runs_dir = lambda: (SANDBOX.mkdir(parents=True, exist_ok=True) or SANDBOX)  # type: ignore[assignment]
        return SANDBOX

    def __exit__(self, *exc):
        store.runs_dir = self._orig                           # type: ignore[assignment]
        return False


class patch_executor:
    def __init__(self, fake) -> None:                         # noqa: ANN001
        self.fake = fake

    def __enter__(self):
        self._orig = E.executor_for
        E.executor_for = lambda node_type: self.fake          # type: ignore[assignment]
        return self.fake

    def __exit__(self, *exc):
        E.executor_for = self._orig                           # type: ignore[assignment]
        return False


def main() -> int:
    c = Checker()
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)

    with patch_store_dir():
        # --------------------------------------------------------------
        c.section("第 1 节：Run 落盘 / 重载 roundtrip")
        # --------------------------------------------------------------
        wf = loader.load_text("""
id: ckpt_demo
nodes:
  - { id: s1, type: data.kb_query, params: { query: q }, side_effect: none }
  - { id: s2, type: data.kb_query, params: { query: q }, side_effect: none, needs: [s1] }
""")
        fake = FakeNode()
        rid = "ckpt-roundtrip-001"
        with patch_executor(fake):
            run1 = E.Engine(wf, persist=True).run(run_id=rid)
        c.check("第一次运行 done", run1.state == StepState.DONE, run1.state.value)
        c.check("两个节点都被执行过", sorted(fake.calls) == ["s1", "s2"], str(fake.calls))

        loaded = store.load_run(rid)
        c.check("★ run 能重载", loaded is not None)
        c.check("★ 重载后状态一致", loaded is not None and loaded.state == StepState.DONE)
        c.check("★ 重载后 steps 数与节点数一致",
                loaded is not None and len(loaded.steps) == 2,
                str(len(loaded.steps)) if loaded else "None")
        c.check("★ 重载后节点产出还在（不是 None）",
                loaded is not None and loaded.steps["s1"].output is not None)

        # --------------------------------------------------------------
        c.section("第 2 节：Checkpoint 内容 —— ★ node_outputs 必须完整")
        # --------------------------------------------------------------
        cp = store.load_checkpoint(rid)
        c.check("★ checkpoint 能重载", cp is not None)
        c.check("★★ checkpoint 里两个节点的产出都在",
                cp is not None and sorted(cp.node_outputs) == ["s1", "s2"],
                str(sorted(cp.node_outputs)) if cp else "None")
        c.check("★ 节点状态也存了（续跑时要据此跳过）",
                cp is not None and cp.node_states.get("s1") == "done",
                str(cp.node_states) if cp else "None")

        # --------------------------------------------------------------
        c.section("第 3 节：★★★ 续跑 —— 已完成节点**不重复执行**")
        # --------------------------------------------------------------
        fake2 = FakeNode()
        with patch_executor(fake2):
            run2 = E.Engine(wf, persist=False).run(run_id=rid, resume=True)
        c.check("★★ **续跑时没有任何节点被重新执行**",
                fake2.calls == [], f"实际又跑了 {fake2.calls}")
        c.check("★ 续跑后整体仍 done", run2.state == StepState.DONE, run2.state.value)

        # --------------------------------------------------------------
        c.section("第 4 节：★ 部分完成的续跑（真的从中间继续）")
        # --------------------------------------------------------------
        rid2 = "ckpt-partial-002"
        # 手工造一个"只完成 s1"的断点
        store.save_checkpoint(Checkpoint(
            run_id=rid2, workflow_id=wf.id,
            node_outputs={"s1": {"v": 42, "from": "断点"}},
            node_states={"s1": "done", "s2": "pending"},
        ))
        fake3 = FakeNode()
        with patch_executor(fake3):
            run3 = E.Engine(wf, persist=False).run(run_id=rid2, resume=True)
        c.check("★★ 只重新执行了 **s2**（s1 从断点恢复）",
                fake3.calls == ["s2"], f"实际 {fake3.calls}")
        c.check("★ 整体 done", run3.state == StepState.DONE, run3.state.value)
        c.check("★★ 下游确实拿到了断点里的上游产出（不是 None）",
                isinstance(run3.steps["s2"].output, dict)
                and run3.steps["s2"].output.get("saw_upstream") is True,
                str(run3.steps["s2"].output))

        # --------------------------------------------------------------
        c.section("第 5 节：★ 原子写入 —— 不留 .tmp 残留")
        # --------------------------------------------------------------
        tmp_left = list(SANDBOX.glob("*.tmp"))
        c.check("★ 沙箱里没有残留的 .tmp 文件", not tmp_left, str(tmp_left))
        c.check("★ run 与 ckpt 文件都生成了",
                (SANDBOX / f"{rid}.json").is_file()
                and (SANDBOX / f"{rid}.ckpt.json").is_file())

        # --------------------------------------------------------------
        c.section("第 6 节：★ 路径安全 —— run_id 不能穿越目录")
        # --------------------------------------------------------------
        for bad in ("../evil", "a/b", "..", "", "x" * 1 + "/../y"):
            c.check(f"非法 run_id {bad!r} → 拒绝",
                    _rejects(bad), "")
        c.check("★ 合法 run_id 能通过", not _rejects("wf-2026-ok.1:2"))

        # --------------------------------------------------------------
        c.section("第 7 节：★ 损坏文件不崩溃（返回 None，区分「没有」与「读坏了」）")
        # --------------------------------------------------------------
        broken = SANDBOX / "broken.json"
        broken.write_text("{ 这不是合法 JSON", encoding="utf-8")
        c.check("★ 损坏的 run 文件 → 返回 None 而不是抛异常",
                store.load_run("broken") is None)
        c.check("★ 不存在的 run → 返回 None",
                store.load_run("no-such-run") is None)
        c.check("★ list_runs 跳过损坏文件（不中断整个列表）",
                isinstance(store.list_runs(), list))

        # --------------------------------------------------------------
        c.section("第 8 节：输出不可 JSON 序列化时降级（不能因为存不下来就丢运行）")
        # --------------------------------------------------------------
        store.save_checkpoint(Checkpoint(
            run_id="ckpt-objjson-003", workflow_id="x",
            node_outputs={"n": object()},          # 不可序列化
        ))
        cp3 = store.load_checkpoint("ckpt-objjson-003")
        c.check("★ 不可序列化的产出被降级为字符串（而不是写盘失败）",
                cp3 is not None and isinstance(cp3.node_outputs.get("n"), str),
                str(cp3.node_outputs) if cp3 else "None")

    # ------------------------------------------------------------------
    shutil.rmtree(SANDBOX, ignore_errors=True)
    print()
    print("=" * 78)
    print(f"断点续跑守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


def _rejects(run_id: str) -> bool:
    try:
        store.load_run(run_id)
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    raise SystemExit(main())