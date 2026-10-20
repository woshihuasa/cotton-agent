# -*- coding: utf-8 -*-
"""测量基础设施守卫 —— 稳定前缀指纹 + token 用量采集。

【定位】`docs/AGENT_RUNTIME.md` 步 1–2 的验收。这两件事的共同点是：
        **它们本身不改变回答，但"回答为什么这么贵"必须靠它们才能说清**。

        因此本守卫守的不是功能，而是**测量的可信度**：
        · 哈希恒定 —— 否则命中率无法归因（每轮哈希都变 = 无法分组统计）
        · 用量字段缺失时**如实说"缺失"**，而不是伪装成 0% 命中

【零 LLM / 零 API】全部是纯函数与假对象断言。

【用法】python tests/check_prompt_layers.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import prompt_layers                              # noqa: E402
from core.llm_client import Usage, _extract_cache_tokens     # noqa: E402
from core.rag_engine import SYSTEM_PROMPT                    # noqa: E402


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


class _FakeUsage:
    """模拟 OpenAI/DeepSeek 的 usage 对象（字段按需拼装）。"""

    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：分层边界存在且分界正确")
    # ------------------------------------------------------------------
    c.check("SYSTEM_PROMPT 含分层边界 {summary_section}",
            prompt_layers.has_boundary(SYSTEM_PROMPT))
    stable = prompt_layers.stable_prefix(SYSTEM_PROMPT, "2026-10-17")
    c.check("稳定段**不含**任何可变段占位符",
            all(m not in stable for m in
                ("{summary_section}", "{l4_section}", "{skill_section}", "{context}")),
            stable[-120:])
    c.check("稳定段确实填入了日期",
            "2026-10-17" in stable)
    c.check("稳定段有实质体量（>1000 字，说明固定规则真的在前）",
            len(stable) > 1000, f"实际 {len(stable)} 字")
    c.check("占位符已被消费（不残留花括号）",
            "{" not in stable.replace("{{", ""), stable[-80:])

    # ------------------------------------------------------------------
    c.section("第 2 节：★ stable_hash 不变量（命中率可归因的前提）")
    # ------------------------------------------------------------------
    d = "2026-10-17"
    h1 = prompt_layers.stable_hash(SYSTEM_PROMPT, d)
    h2 = prompt_layers.stable_hash(SYSTEM_PROMPT, d)
    c.check("同一日期内两次计算**完全相同**", h1 == h2)
    c.check("是 64 位十六进制（sha256）",
            len(h1) == 64 and all(ch in "0123456789abcdef" for ch in h1), h1)

    h_other = prompt_layers.stable_hash(SYSTEM_PROMPT, "2026-10-18")
    c.check("★ 日期变化 → 哈希必须变化（证明日期确实被算进稳定段）",
            h_other != h1)

    # 守卫的核心断言：可变内容**不得**影响哈希
    variants = [
        {}, {"summary_section": "【历史摘要】x" * 50},
        {"l4_section": "【用户长期记忆】y" * 50},
        {"skill_section": "【技能 · 编排指导】z" * 50},
        {"context": "【背景知识】w" * 50},
        {"summary_section": "a", "l4_section": "b", "skill_section": "c", "context": "d"},
    ]
    hashes = {prompt_layers.stable_hash(SYSTEM_PROMPT, d) for _ in variants}
    c.check("★ 给 6 组完全不同的可变段，stable_hash **全同**（前缀不受易变内容影响）",
            len(hashes) == 1, str(len(hashes)))

    # ------------------------------------------------------------------
    c.section("第 3 节：前缀不变量（「缓存有效」在代码里的直接表达）")
    # ------------------------------------------------------------------
    today = "2026-10-17"
    system_content = SYSTEM_PROMPT.format(
        today_date=today,
        summary_section="【历史摘要】可变A", l4_section="【用户长期记忆】可变B",
        skill_section="【技能 · 编排指导】可变C", context="【背景知识】可变D",
    )
    c.check("★ 组装出的 system prompt **以稳定段开头**",
            prompt_layers.verify_prefix(system_content, SYSTEM_PROMPT, today))
    c.check("反例：换一个日期 → 前缀校验必须为假（断言不是恒真）",
            not prompt_layers.verify_prefix(system_content, SYSTEM_PROMPT, "2026-10-18"))
    c.check("反例：把可变内容插到最前 → 校验为假",
            not prompt_layers.verify_prefix("【历史摘要】x" + system_content, SYSTEM_PROMPT, today))

    # ------------------------------------------------------------------
    c.section("第 4 节：分层审计")
    # ------------------------------------------------------------------
    sizes = prompt_layers.layer_sizes(
        SYSTEM_PROMPT, today_date=today, summary_section="S" * 10,
        l4_section="L" * 20, skill_section="K" * 30, context="C" * 40,
    )
    c.check("五个源齐全（防「某源静默消失」）",
            set(sizes) == {"stable", "l3_summary", "l4_memory", "skills", "kb_context"},
            str(sorted(sizes)))
    c.check("各源字符数与入参一致",
            (sizes["l3_summary"], sizes["l4_memory"], sizes["skills"], sizes["kb_context"])
            == (10, 20, 30, 40), str(sizes))
    c.check("describe 输出含占比",
            "%" in prompt_layers.describe_layer_sizes(sizes),
            prompt_layers.describe_layer_sizes(sizes))
    c.check("空输入不除零",
            isinstance(prompt_layers.describe_layer_sizes(
                {"stable": 0, "l3_summary": 0, "l4_memory": 0,
                 "skills": 0, "kb_context": 0}), str))

    # ------------------------------------------------------------------
    c.section("第 5 节：token 用量采集（三种取值路径 + 缺失时不伪装）")
    # ------------------------------------------------------------------
    hit, miss = _extract_cache_tokens(_FakeUsage(
        prompt_tokens=1000, prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200))
    c.check("① DeepSeek 原生字段被正确读取", (hit, miss) == (800, 200), f"{hit}/{miss}")

    hit2, miss2 = _extract_cache_tokens(_FakeUsage(
        prompt_tokens=1000, prompt_tokens_details=_FakeUsage(cached_tokens=600)))
    c.check("② OpenAI 风格 cached_tokens 被读取，未命中由总额反推",
            (hit2, miss2) == (600, 400), f"{hit2}/{miss2}")

    hit3, miss3 = _extract_cache_tokens(_FakeUsage(
        prompt_tokens=500, model_extra={"prompt_cache_hit_tokens": 100,
                                        "prompt_cache_miss_tokens": 400}))
    c.check("③ model_extra 里的字段被读取", (hit3, miss3) == (100, 400), f"{hit3}/{miss3}")

    c.check("无缓存字段 → 返回 (0,0) 而不是抛异常",
            _extract_cache_tokens(_FakeUsage(prompt_tokens=500)) == (0, 0))
    c.check("usage 为 None → 安全返回 (0,0)",
            _extract_cache_tokens(None) == (0, 0))
    c.check("字段是脏数据（字符串）→ 不抛异常",
            _extract_cache_tokens(_FakeUsage(
                prompt_cache_hit_tokens="坏", prompt_cache_miss_tokens=None)) == (0, 0))

    u = Usage(prompt_tokens=1000, completion_tokens=50,
              cache_hit_tokens=800, cache_miss_tokens=200, calls=1)
    c.check("命中率计算正确（800/1000 = 80%）",
            abs(u.cache_hit_rate - 0.8) < 1e-9, str(u.cache_hit_rate))
    c.check("prompt_total 优先用 prompt_tokens",
            u.prompt_total == 1000, str(u.prompt_total))

    u_nocache = Usage(prompt_tokens=1000, completion_tokens=50, calls=1)
    c.check("无缓存数据时命中率为 0.0（**不是**猜一个数）",
            u_nocache.cache_hit_rate == 0.0)
    c.check("★ 无缓存字段时 describe **如实说「缺失」**，不伪装成 0% 命中",
            "缓存字段缺失" in u_nocache.describe(), u_nocache.describe())
    c.check("有缓存字段时 describe 给出命中率",
            "命中率 80.0%" in u.describe(), u.describe())
    c.check("零调用时 describe 说「无调用」", Usage().describe() == "无调用")

    u2 = u + Usage(prompt_tokens=500, completion_tokens=10,
                   cache_hit_tokens=100, cache_miss_tokens=400, calls=1)
    c.check("累计相加：字段与次数都对",
            (u2.prompt_tokens, u2.calls, u2.cache_hit_tokens) == (1500, 2, 900),
            f"{u2.prompt_tokens}/{u2.calls}/{u2.cache_hit_tokens}")

    # ------------------------------------------------------------------
    c.section("★ 第 N 节：LLMClient 实例属性完整（填一个真实的守卫真空）")
    # ------------------------------------------------------------------
    # 【为什么补这一节】2026-10 的一次改动把 `_sampling_kwargs` 插进了 `__init__` 中间，
    # 使用量计量的三行**落到 return 之后成为死代码** —— 而当时 **19 个守卫全绿**，
    # 因为**没有任何零成本守卫构造过 LLMClient 并触发用量记录**。
    # 这个 bug 只在真跑评测时暴露（`'LLMClient' object has no attribute '_usage_lock'`），
    # 代价是一次 20 条样本的完整链路。
    # 本节就是那次的补丁：**构造即检查属性**，零成本、零网络。
    from core.llm_client import LLMClient

    cl = LLMClient()          # 不发起任何请求，只是构造
    required = ("_model", "_client", "_extra_body", "_temperature",
                "last_usage", "usage_total", "_usage_lock")
    missing = [a for a in required if not hasattr(cl, a)]
    c.check(f"★ LLMClient 构造后具备全部 {len(required)} 个实例属性",
            not missing, f"缺失: {missing}")
    c.check("★ `usage_total` 是可用的 Usage 实例（不是 None）",
            cl.usage_total is not None and hasattr(cl.usage_total, "prompt_tokens"))
    c.check("★ `_usage_lock` 有 acquire/release（是真锁，不是占位）",
            hasattr(cl._usage_lock, "acquire") and hasattr(cl._usage_lock, "release"))
    c.check("构造后 `last_usage` 为 None（尚未调用过）", cl.last_usage is None)
    c.check("`_sampling_kwargs()` 默认为空 dict（不传任何采样参数）",
            cl._sampling_kwargs() == {}, str(cl._sampling_kwargs()))
    c.check("★ 默认 temperature 为 None（= 不传该参数，行为与改动前一致）",
            cl._temperature is None, str(cl._temperature))

    # 显式设置后应能反映出来
    cl2 = LLMClient(temperature=0.2, extra_body={"seed": 42})
    kw = cl2._sampling_kwargs()
    c.check("显式设 temperature/extra_body 后，采样参数正确带出",
            kw.get("temperature") == 0.2 and kw.get("extra_body") == {"seed": 42}, str(kw))
    c.check("★ 两个实例的采样设置互不影响（extra_body 是拷贝，不是共享引用）",
            cl._sampling_kwargs() == {} and cl2._sampling_kwargs() != {})

    # ------------------------------------------------------------------
    c.section("★ 缓存命中提示（输入框下方小字，纯函数）")
    # ------------------------------------------------------------------
    from core.prompt_layers import CACHE_HINT_NO_DATA, format_cache_hint

    txt, tip = format_cache_hint(1024, 210, model="deepseek-v4.1-flash")
    c.check("有数据 → 显示百分比（取整）", txt == "缓存命中 83%", txt)
    c.check("悬停里有明细（命中/未命中）", "命中 1024" in tip and "未命中 210" in tip, tip)
    c.check("悬停里带模型名", "deepseek-v4.1-flash" in tip)
    c.check("不传模型时悬停不带「模型：」", "模型：" not in format_cache_hint(1, 1)[1])

    # ★★ 核心断言：无数据 ≠ 0%
    txt0, tip0 = format_cache_hint(0, 0)
    c.check(f"★★ **无数据 → 显示占位符「{CACHE_HINT_NO_DATA}」，而不是「0%」**",
            txt0 == CACHE_HINT_NO_DATA and "0%" not in txt0, txt0)
    c.check("★ 无数据时悬停**明确说明**不代表 0%",
            "不代表命中率为 0" in tip0, tip0)
    txtz, _ = format_cache_hint(0, 900)
    c.check("★ **真的 0%（有未命中）→ 才显示「0%」**", txtz == "缓存命中 0%", txtz)
    c.check("负数/None 输入不抛异常",
            format_cache_hint(-5, None)[0] == CACHE_HINT_NO_DATA)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"测量基础设施守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())