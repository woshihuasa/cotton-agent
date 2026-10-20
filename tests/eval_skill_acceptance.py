# -*- coding: utf-8 -*-
"""技能验收用例自动跑 —— 工具选择层 + 结晶档首次触发。

【定位】ARCHITECTURE.md §8.9 的 ⏳ 项：之前只校验 acceptance 的**结构**，
        现在校验**通过率**。工具选择需要 LLM（无法零成本），
        3 个技能 × 2 条用例 = 6 次 chat_with_tools 调用，成本可忽略。

【同时完成两件事】
        ① 验收：对每条 acceptance，断言模型确实选了 expected_tools
        ② 结晶档首次触发：ask() 的工具循环会记录轨迹到
           procedural_memory/index.jsonl —— 这是该文件的**首次真实写入**

【隔离策略】
        · 验收部分用沙箱引擎（`user_data_dir=sandbox`），不碰生产
        · 结晶触发用 `owns_user_data=False`：不写 L4 记忆、不写会话存档，
          但结晶档路径走生产默认（`app_data_dir()/procedural_memory/index.jsonl`）
        → 这样结晶首次写入发生在生产位置，而其余生产数据不受影响

【用法】python tests/eval_skill_acceptance.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from config import skill_mount_dirs                      # noqa: E402
from core.rag_engine import RAGEngine                    # noqa: E402
from core.skill_loader import SkillLibrary               # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "skill_acceptance_sandbox"


def _stub_engine(engine: RAGEngine, answer: str = "这是最终回答。") -> None:
    """桩件：省 LLM（只留 chat_with_tools 跑真的）、省 embedding、省后台线程。"""
    engine._rewrite_query = lambda q: q                  # type: ignore
    engine.kb.retrieve_context = lambda *a, **k: ""      # type: ignore
    engine.memory.retrieve_l4_memory = lambda *a, **k: []  # type: ignore
    engine.memory.retrieve_session_summaries = lambda *a, **k: []  # type: ignore
    engine._generate_title = lambda *a, **k: None        # type: ignore
    engine._start_memory_worker = lambda *a, **k: None   # type: ignore

    calls = {"stream": 0}

    def _stream(messages, model_name=None, enable_thinking=False,
                reasoning_effort=None):
        calls["stream"] += 1
        yield ("content", answer)

    engine.llm.stream_response = _stream                 # type: ignore


def _run_acceptance() -> tuple[int, int, list[str]]:
    """跑全部技能的 acceptance 用例，返回 (通过数, 总数, 详情行)。"""
    lib = SkillLibrary(mount_dirs=skill_mount_dirs())
    if lib.errors():
        print(f"[WARN] 技能解析有错误: {lib.errors()}")

    cases: list[tuple[Skill, dict]] = []
    for sk in lib.all_skills():
        for case in sk.acceptance:
            cases.append((sk, case))

    if not cases:
        print("[FAIL] 没有可跑的 acceptance 用例")
        return 0, 0, []

    print(f"共 {len(cases)} 条 acceptance 用例（来自 {len(lib.all_skills())} 个技能）")
    print("=" * 78)

    passed = 0
    details: list[str] = []

    for sk, case in cases:
        question = case["question"]
        expected = set(case.get("expected_tools") or [])

        # 沙箱引擎
        if SANDBOX.exists():
            import shutil
            shutil.rmtree(SANDBOX, ignore_errors=True)
        SANDBOX.mkdir(parents=True, exist_ok=True)
        engine = RAGEngine(user_data_dir=SANDBOX)
        _stub_engine(engine)
        engine.create_session()

        # 跑一轮 ask()，捕获工具调用
        tools_called: list[str] = []
        original_exec = engine._execute_tool

        def _capture_exec(name, args):
            tools_called.append(name)
            return original_exec(name, args)

        engine._execute_tool = _capture_exec              # type: ignore

        try:
            # ask() 是生成器，需要消费完
            for _ in engine.ask(question):
                pass
        except Exception as e:
            print(f"  [ERR ] {sk.id}: {question[:24]} ← 异常 {type(e).__name__}: {e}")
            details.append(f"[ERR] {sk.id} '{question[:24]}' ← {e}")
            continue

        got = set(tools_called)
        ok = expected.issubset(got) if expected else bool(got)
        passed += int(ok)

        mark = "PASS" if ok else "FAIL"
        missing = expected - got if expected else set()
        extra = got - expected if expected else got
        line = f"  [{mark}] {sk.id}: {question[:28]}"
        if ok:
            line += f"  → {sorted(got)}"
        else:
            if missing:
                line += f"  ← 缺 {sorted(missing)}"
            if extra:
                line += f"  多 {sorted(extra)}"
        print(line)
        details.append(line.strip())

    return passed, len(cases), details


def _trigger_crystallization() -> dict:
    """用 owns_user_data=False 的引擎跑一个问题，触发结晶档首次写入。

    owns_user_data=False → 不写 L4 记忆、不写会话存档，
    但结晶档路径走生产默认（app_data_dir()/procedural_memory/index.jsonl）。
    """
    from config import app_data_dir
    crystal_path = Path(app_data_dir()) / "procedural_memory" / "index.jsonl"
    existed_before = crystal_path.exists()
    entries_before = 0
    if existed_before:
        entries_before = sum(1 for line in crystal_path.read_text("utf-8").splitlines()
                             if line.strip())

    engine = RAGEngine(owns_user_data=False)
    _stub_engine(engine)
    engine.create_session()

    question = "今天阿拉尔适合打药吗"
    print(f"\n触发问题: {question}")
    for _ in engine.ask(question):
        pass

    existed_after = crystal_path.exists()
    entries_after = 0
    if existed_after:
        entries_after = sum(1 for line in crystal_path.read_text("utf-8").splitlines()
                            if line.strip())

    # 读取最后写入的条目（用于诊断展示）
    sample = ""
    if existed_after:
        lines = [ln for ln in crystal_path.read_text("utf-8").splitlines() if ln.strip()]
        if lines:
            try:
                obj = json.loads(lines[-1])
                sample = f"{obj.get('signature', '?')} (hit_count={obj.get('hit_count', '?')})"
            except json.JSONDecodeError:
                sample = "(末行非 JSON)"

    return {
        "path": str(crystal_path.relative_to(ROOT)) if crystal_path.is_relative_to(ROOT)
                else str(crystal_path),
        "existed_before": existed_before,
        "entries_before": entries_before,
        "existed_after": existed_after,
        "entries_after": entries_after,
        "new_entries": entries_after - entries_before,
        "sample": sample,
    }


def main() -> int:
    print("=" * 78)
    print("技能验收用例自动跑 —— 工具选择层 + 结晶档首次触发")
    print("=" * 78)

    # ---- Part A: acceptance ----
    print("\n[Part A] 验收用例（需 LLM）")
    passed, total, details = _run_acceptance()

    print()
    print("-" * 78)
    if total:
        rate = passed / total * 100
        print(f"工具选择通过率: {passed}/{total} = {rate:.1f}%")
    else:
        print("无可用用例")
    print("-" * 78)

    # ---- Part B: 结晶档首次触发 ----
    print("\n[Part B] 结晶档首次触发（owns_user_data=False，走生产路径）")
    result = _trigger_crystallization()

    print()
    print(f"  结晶档路径:        {result['path']}")
    print(f"  触发前存在:        {result['existed_before']}"
          f"（{result['entries_before']} 条）" if result['existed_before'] else "")
    print(f"  触发后存在:        {result['existed_after']}")
    print(f"  触发后条目数:      {result['entries_after']}")
    print(f"  新增条目:          {result['new_entries']}")
    print(f"  末条样例:          {result['sample']}")

    # ---- 判定 ----
    print()
    print("=" * 78)
    acceptance_ok = total > 0 and passed == total
    crystal_ok = result["existed_after"] and result["entries_after"] > 0
    print(f"验收通过率:         {'达标 ✓' if acceptance_ok else '有失败 ✗'}"
          f"（{passed}/{total}）")
    print(f"结晶档首次写入:     {'成功 ✓' if crystal_ok else '未写入 ✗'}")
    print("=" * 78)
    return 0 if acceptance_ok and crystal_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())