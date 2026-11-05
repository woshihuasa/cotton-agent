# -*- coding: utf-8 -*-
"""查询改写守卫 —— 守住"拿坏输入去检索"这件事不会发生。

【为什么这条链路必须单独守】改写的输出直接当检索查询用。它一旦被污染，
失败是**静默的**：召回变差、答案变差，全程不报错，日志里也看不出来。
所以判断规则本身就是需要被守住的对象 —— 规则被谁顺手放松一点，没有任何别的
地方会发现。

【四种污染形态】
  ① 模型拒答（"抱歉，我无法…"）→ 拿一句道歉去检索；
  ② 模型直接回答（"可以，建议在 28 日之前…"）→ 拿一段答案去检索；
  ③ 模型输出多句解释 → 检索被无关词淹没；
  ④ API 失败被伪装成内容（`generate_response` 返回"抱歉，AI 服务暂时不可用…"）。

【用法】python tests/check_query_rewrite.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# 只导入静态方法，不构造引擎（构造会加载向量模型）
from core.rag_engine import RAGEngine                     # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}"
              + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def valid(q: str, r: str) -> bool:
    return RAGEngine._rewrite_looks_valid(q, r)[0]


def reason(q: str, r: str) -> str:
    return RAGEngine._rewrite_looks_valid(q, r)[1]


def section_accept(c: Checker) -> None:
    c.section("第 1 节：正常的改写必须放行（否则等于关掉了改写）")

    cases = [
        ("它的价格是多少", "2024 年新疆棉花 3128B 现货价格是多少"),
        ("棉花什么时候打顶", "新疆棉花打顶时间与最晚期限"),
        ("它的用量呢", "缩节胺在棉花上的常规用量是多少"),
        ("这两天能打顶吗", "阿克苏地区 11 月初是否适合棉花打顶"),
        ("那价格呢", "2026 年 11 月 3 日棉花现货与郑棉期货价格"),
    ]
    for q, r in cases:
        c.check(f"放行：{r[:20]}…", valid(q, r), reason(q, r))

    c.check("短问题也能放行（长度上限按比例放宽）",
            valid("多少钱", "棉花价格"), reason("多少钱", "棉花价格"))


def section_refusal(c: Checker) -> None:
    c.section("第 2 节：★ 拒答的各种写法都要拦住")

    variants = [
        "抱歉，我无法回答这个问题。",
        "对不起，我不能提供该信息",
        "很抱歉，我无法完成这个改写。",
        "我无法根据这些信息判断。",
        "无法回答：信息不足。",
        "作为一个语言模型，我不能给出建议。",
    ]
    for r in variants:
        c.check(f"拦截：{r[:18]}…", not valid("它多少钱", r), "被放行了")
    c.check("★ 拒绝理由写明是拒答", "拒答" in reason("多少钱", "抱歉，我做不到。"),
            reason("多少钱", "抱歉，我做不到。"))


def section_answer(c: Checker) -> None:
    c.section("第 3 节：★ 把回答当改写也要拦住")

    c.check("以「可以」开头 → 拦截",
            not valid("这两天能打顶吗", "可以，建议在 28 日之前完成打顶"))
    c.check("以「建议」开头 → 拦截",
            not valid("怎么施肥", "建议每亩追施尿素 10 公斤"))
    c.check("多句（两个以上句号）→ 拦截",
            not valid("怎么施肥", "先施底肥。再追肥。最后叶面喷施。"))
    c.check("含换行（像是解释）→ 拦截",
            not valid("它多少钱", "改写结果如下\n2024 年棉花价格"))
    c.check("长度暴涨 → 拦截",
            not valid("它多少钱", "棉花" * 60),
            reason("它多少钱", "棉花" * 60))
    c.check("空输出 → 拦截", not valid("它多少钱", ""))
    c.check("只有空白 → 拦截", not valid("它多少钱", "   \n  "))


def section_boundary(c: Checker) -> None:
    c.section("第 4 节：边界与误判代价")

    # 一个句号是可以的（问句里偶尔带一句限定），两个才判回答
    c.check("★ 一个句号仍放行（避免误伤）",
            valid("它多少钱", "2024 年新疆棉花价格。3128B"),
            reason("它多少钱", "2024 年新疆棉花价格。3128B"))
    # 长问题下，较长的改写仍应放行，只是会被截断到 400
    long_q = "请问在阿克苏地区十一月初这段时间夜间温度已经降到零度附近的情况下" * 3
    long_r = "阿克苏地区 11 月初夜间低温接近 0℃ 时是否还适合进行棉花打顶作业"
    c.check("长问题对应的长改写放行", valid(long_q, long_r), reason(long_q, long_r))

    src = (ROOT / "core" / "rag_engine.py").read_text(encoding="utf-8")
    c.check("★ 截断到 400 字符（防止超长查询拖慢检索）",
            "[:400]" in src, "")
    c.check("★ 回退时写了 warning 日志（否则无法区分模型跑偏与接口失败）",
            "查询改写被丢弃" in src, "")


def section_failure_is_content(c: Checker) -> None:
    c.section("第 5 节：★ API 失败被伪装成内容 —— 这条规则真正的靠山")

    src = (ROOT / "core" / "llm_client.py").read_text(encoding="utf-8")
    c.check("`generate_response` 失败时返回一句道歉（而不是抛错）",
            'return "抱歉，AI 服务暂时不可用' in src, "")
    c.check("这条道歉以「抱歉」开头，所以会被改写守则拦下",
            valid("它多少钱", "抱歉，AI 服务暂时不可用，请检查网络或 API 配置。")
            is False, "")

    # 规则不能只认"抱歉"：中文拒答还有别的开头
    prefixes = RAGEngine._REWRITE_REFUSAL_PREFIXES
    c.check("拒答前缀至少有四种写法", len(prefixes) >= 4, str(prefixes))
    c.check("「对不起」在名单里（不以「抱歉」开头）", "对不起" in prefixes)
    c.check("「很抱歉」在名单里（「抱歉」匹配不到它）", "很抱歉" in prefixes)

    openers = RAGEngine._REWRITE_ANSWER_OPENERS
    c.check("答案口吻词表存在且不少于六项", len(openers) >= 6, str(openers))


def section_usage(c: Checker) -> None:
    c.section("第 6 节：函数确实接上了判断（不是写了没用）")

    src = (ROOT / "core" / "rag_engine.py").read_text(encoding="utf-8")
    body = src[src.index("def _rewrite_query"):]
    body = body[:body.index("return rewritten.strip()[:400]") + 30]
    c.check("调用方调用了 `_rewrite_looks_valid`",
            "_rewrite_looks_valid(question, rewritten)" in body, "")
    c.check("不通过时返回原问题", "return question" in body, "")
    c.check("首轮无历史时直接跳过（省一次调用）",
            "if not session.conversation_store:" in body, "")

    # 旧的单前缀判断必须已被替换，否则等于两套规则并存
    c.check("★ 旧的 `startswith(\"抱歉\")` 单行判断已移除",
            not re.search(r"^\s*if rewritten\.startswith\(\"抱歉\"\)",
                          src, re.M), "")


def main() -> int:
    c = Checker()
    try:
        section_accept(c)
        section_refusal(c)
        section_answer(c)
        section_boundary(c)
        section_failure_is_content(c)
        section_usage(c)
    finally:
        print()
        print("=" * 78)
        print(f"查询改写守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())