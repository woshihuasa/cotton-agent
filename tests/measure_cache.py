# -*- coding: utf-8 -*-
"""实测 DeepSeek 上下文缓存命中率 —— 决定「提示分层」叙事是否成立。

【为什么需要这一步】`docs/AGENT_RUNTIME.md` 的 A2 声称"本项目 prompt 结构已天然适配前缀缓存"。
但**声称不是证据**。本脚本用真实调用测出命中率，据实决定后续：

  · 若命中率显著 > 0  → 叙事成立：「发现结构已适配 → 加测量与守卫把它锁住」
  · 若命中率 ≈ 0      → 叙事不成立，应如实报告，并考虑调整段序（§2.2 步骤⑤）
  · 若缓存字段缺失     → 如实报告"该接口未回传该字段"，不伪装成 0%

【实验设计】缓存按 **prompt 前缀逐字节相同** 判定，所以**第一次调用必然是未命中**
（它负责建立缓存），命中要从**第二次**开始看。因此：
  跑 N 个**不同的问题**（稳定前缀相同、易变段不同），逐次打印计量，
  再对比"第 1 次"与"后续"的命中情况。

【隔离】用 `owns_user_data=False`：不写 L4 记忆、不写会话存档；同时桩掉标题生成与
L4 worker，避免它们额外的 LLM 调用污染计量。

【用法】python tests/measure_cache.py [问题数，默认 3]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.llm_client import Usage                     # noqa: E402
from core.rag_engine import RAGEngine                 # noqa: E402

# 刻意选**不同形态**的问题：一个要调工具、一个要检索、一个偏闲聊。
# 目的是让易变段（L4 / 技能 / 背景知识）尽量不同 —— 从而证明
# "易变段不同也不影响命中"，即命中确实发生在前缀上。
QUESTIONS = [
    "今天阿拉尔适合打药吗",
    "棉花苗期需要怎么施肥",
    "你好",
]


def _prep(engine: RAGEngine) -> None:
    """桩掉会额外花 LLM 调用的部分，保证计量只反映问答本身。"""
    engine._generate_title = lambda *a, **k: None            # type: ignore[method-assign]
    engine._start_memory_worker = lambda *a, **k: None       # type: ignore[method-assign]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    questions = QUESTIONS[:n]

    print("=" * 78)
    print("DeepSeek 上下文缓存命中率实测")
    print("=" * 78)

    engine = RAGEngine(owns_user_data=False)
    _prep(engine)
    engine.create_session()

    rows: list[tuple[str, Usage]] = []
    for i, q in enumerate(questions, 1):
        before: Usage = engine.llm.usage_total
        engine.llm.reset_usage(); engine.llm.usage_total = before   # 只清 last，保留累计
        try:
            for _ in engine.ask(q):
                pass
        except Exception as e:                                       # noqa: BLE001
            print(f"  第 {i} 次 异常: {type(e).__name__}: {e}")
            continue
        after: Usage = engine.llm.usage_total
        delta = Usage(
            prompt_tokens=after.prompt_tokens - before.prompt_tokens,
            completion_tokens=after.completion_tokens - before.completion_tokens,
            cache_hit_tokens=after.cache_hit_tokens - before.cache_hit_tokens,
            cache_miss_tokens=after.cache_miss_tokens - before.cache_miss_tokens,
            calls=after.calls - before.calls,
        )
        rows.append((q, delta))
        print(f"\n  第 {i} 次 {q!r}")
        print(f"    {delta.describe()}")

    # ---- 汇总 ----
    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    total = Usage()
    for _q, d in rows:
        total = total + d
    print(f"  全部调用: {total.describe()}")

    if total.cache_hit_tokens or total.cache_miss_tokens:
        if len(rows) >= 2:
            rest = Usage()
            for _q, d in rows[1:]:
                rest = rest + d
            print()
            print("  ★ 第 1 次（负责建立缓存）与后续对比：")
            print(f"    第 1 次: 命中 {rows[0][1].cache_hit_tokens} / "
                  f"未命中 {rows[0][1].cache_miss_tokens}")
            print(f"    后续 {len(rows) - 1} 次: 命中 {rest.cache_hit_tokens} / "
                  f"未命中 {rest.cache_miss_tokens} → 命中率 {rest.cache_hit_rate:.1%}")
            print()
            if rest.cache_hit_rate > 0:
                print("  ⇒ 结论：**前缀缓存确实命中**，A2 的叙事成立（「证明并锁住」）。")
            else:
                print("  ⇒ 结论：后续仍未命中。**如实报告**，需按 §2.2 步骤⑤ 检查段序。")
    else:
        print()
        print("  ⚠️ 该接口**未回传缓存字段** —— 如实报告，不伪装成 0% 命中。")
        print("     （可从 §2.2 步骤① 的兼容路径确认字段名，或改用其它计量口径）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())