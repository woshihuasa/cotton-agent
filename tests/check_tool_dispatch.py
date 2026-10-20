# -*- coding: utf-8 -*-
"""工具调度三层防线守卫 —— 零 LLM、零网络、零写盘。

【定位】`docs/ROADMAP.md` 的 **C4**（三道防线）。守的是"工具调错时不静默"。

【为什么这个守卫值得存在】
        三道防线**都不会让程序崩**，所以它们坏掉时**没有任何报错** ——
        症状只是"回答偶尔变差"。这正是本项目 §8.7 记过的那类**静默失效**。
        所以每一道防线都必须有**能被机器验证**的断言。

【主力仍是反例】每道防线都造"模型真的会犯的错"，断言它**被正确识别**；
        同时造"看起来像错、其实不该拦"的样本，断言**不误杀**。

【用法】python tests/check_tool_dispatch.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.tools import (                                      # noqa: E402
    TOOL_HANDLERS, dispatch, execute_tool_result,
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


AVAIL = list(TOOL_HANDLERS)


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：防线一 · 名字解析 —— 别名归一")
    # ------------------------------------------------------------------
    norm_cases = [
        ("web_search", "web_search", "精确命中"),
        ("websearch", "web_search", "去下划线"),
        ("WebSearch", "web_search", "大小写"),
        ("web-search", "web_search", "连字符"),
        ("web search", "web_search", "空格"),
        ("search", "web_search", "别名"),
        ("weather", "get_weather", "别名"),
        ("stats", "query_cotton_stats", "别名"),
        ("cotton_stats", "query_cotton_stats", "别名"),
        ("plot", "plot_trend", "别名"),
        ("chart", "plot_trend", "别名"),
        ("forecast", "analyze_yield", "别名"),
        ("volatility", "calc_price_volatility", "别名"),
    ]
    for raw, want, note in norm_cases:
        r = dispatch.resolve_tool_name(raw, AVAIL)
        c.check(f"★ {note}: {raw!r} → {want}", r.ok and r.canonical == want,
                f"实际 {r.canonical!r} ok={r.ok}")

    # 别名表里的目标必须都真实存在（防"别名指向已删除的工具"）
    missing = [v for v in dispatch.TOOL_ALIASES.values() if v not in AVAIL]
    c.check("★★ 别名表里的目标**全部真实存在**（防指向已删除工具）",
            not missing, f"悬空别名: {missing}")

    # ★★ 别名表的**归一化索引**必须覆盖全部别名。
    # 第一版直接查原表（键带下划线），导致所有带下划线的别名失效 ——
    # 这里专门守「可读的键」与「可查的索引」两件事都得成立。
    uncovered = [k for k in dispatch.TOOL_ALIASES
                 if dispatch._ALIAS_INDEX.get(dispatch.normalize_name(k))
                 != dispatch.TOOL_ALIASES[k]]
    c.check("★★ 别名索引覆盖**每一个**别名（含带下划线的键）",
            not uncovered, f"未覆盖: {uncovered}")
    c.check("★ 带下划线的别名能解析（cotton_stats → query_cotton_stats）",
            dispatch.resolve_tool_name("cotton_stats", AVAIL).canonical
            == "query_cotton_stats")
    c.check("★ 归一化形式也能解析（cottonstats）",
            dispatch.resolve_tool_name("cottonstats", AVAIL).canonical
            == "query_cotton_stats")

    # ------------------------------------------------------------------
    c.section("第 2 节：防线一 · 拼错时必须**报错**，不能猜")
    # ------------------------------------------------------------------
    for typo in ("no_such_tool", "query_cotton_stat", "plot_trnd", ""):
        r = dispatch.resolve_tool_name(typo, AVAIL)
        c.check(f"★ {typo!r} → 不解析（不替模型猜）", not r.ok, f"竟然解析成了 {r.canonical!r}")
        c.check(f"   {typo!r} → 有纠正提示", bool(r.message))

    # 纠正提示必须**带候选**（这是"可纠正"的关键）
    msg = dispatch.resolve_tool_name("query_cotton_stat", AVAIL).message
    c.check("★★ 提示里给出最接近的候选 query_cotton_stats",
            "query_cotton_stats" in msg, msg[:100])
    c.check("★ 提示里列出全部可用工具", all(t in msg for t in AVAIL))
    c.check("★ 首句保留稳定前缀「工具未找到」（既有守卫依赖它）",
            msg.startswith("工具未找到"))
    c.check("★ 提示里明确要求「重新调用」（行动指引而非死路）", "重新调用" in msg)

    # ------------------------------------------------------------------
    c.section("第 3 节：★★ 刻意**不回落默认工具**（与 AgentGPT 的关键分歧）")
    # ------------------------------------------------------------------
    # 未知工具绝不能静默跑成别的工具 —— 那会给出"看起来正常、实际无关"的答案
    r = execute_tool_result("no_such_tool", {})
    c.check("★★ 未知工具 → 返回错误文本，**不是**某个真实工具的结果",
            "工具未找到" in r.text, r.text[:80])
    c.check("★ 且不含任何工具的正常输出特征（没有真的执行）",
            "温度" not in r.text and "chart_path" not in r.text)

    # 别名**真的能跑通**（归一不只是字符串变换）
    r2 = execute_tool_result("weather", {"city": "阿克苏", "date": "2026-10-18"})
    c.check("★ 别名 weather 真的执行了 get_weather",
            "工具未找到" not in r2.text, r2.text[:80])

    # ------------------------------------------------------------------
    c.section("第 4 节：防线二 · 参数多级降级")
    # ------------------------------------------------------------------
    ok_cases = [
        ('{"a": 1}', dispatch.LEVEL_JSON, "标准 JSON"),
        ('  {"a": 1}  ', dispatch.LEVEL_JSON, "前后空白"),
        ('```json\n{"a": 1}\n```', dispatch.LEVEL_FENCED, "```json 围栏"),
        ('```\n{"a": 1}\n```', dispatch.LEVEL_FENCED, "无语言围栏"),
        ('参数如下：\n{"a": 1}\n请执行', dispatch.LEVEL_EMBEDDED, "夹说明文字"),
        ("{'a': 1, 'b': True, 'c': None}", dispatch.LEVEL_LITERAL, "Python 字面量"),
        ("", dispatch.LEVEL_JSON, "空串 = 无参数"),
        (None, dispatch.LEVEL_JSON, "None"),
    ]
    for raw, want_level, note in ok_cases:
        p = dispatch.parse_tool_arguments(raw)
        c.check(f"★ {note} → 解析成功（level={want_level}）",
                p.ok and p.level == want_level, f"ok={p.ok} level={p.level}")

    c.check("★ 嵌套对象能正确解析",
            dispatch.parse_tool_arguments('{"a": {"b": [1, 2]}}').args
            == {"a": {"b": [1, 2]}})
    c.check("★ 中文字符不被转义破坏",
            dispatch.parse_tool_arguments('{"城市": "阿克苏"}').args == {"城市": "阿克苏"})

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ 解析失败时**绝不伪造空参数**（修静默 `{}`）")
    # ------------------------------------------------------------------
    bad_cases = [
        ('{"a": 1', "截断"),
        ("完全不是 JSON", "垃圾文本"),
        ("[1, 2, 3]", "数组不是对象"),
        ("{not json at all}", "花括号里不是 JSON"),
    ]
    for raw, note in bad_cases:
        p = dispatch.parse_tool_arguments(raw)
        c.check(f"★★ {note} → **失败**（ok=False）", not p.ok, f"竟然成功了: {p.args}")
        c.check(f"   {note} → 有可回灌的错误文本", bool(p.error))
        c.check(f"   {note} → args 是空 dict 但**调用方不得使用**（ok=False 已标明）",
                p.args == {} and not p.ok)

    # 错误文本要能指导模型改（这是"回灌"的意义）
    p = dispatch.parse_tool_arguments('{"a": 1')
    c.check("★ 错误文本里**复述了模型发的内容**（便于它自查）",
            "{\"a\": 1" in p.error, p.error[:80])
    c.check("★ 错误文本里说明**正确格式要求**", "JSON" in p.error)

    # ------------------------------------------------------------------
    c.section("第 6 节：防线三 · 重复调用短路")
    # ------------------------------------------------------------------
    g = dispatch.LoopGuard(max_repeat=1)
    args = {"city": "阿克苏", "date": "2026-10-18"}
    c.check("★ 首次调用不拦", g.check("get_weather", args) is None)
    g.remember("get_weather", args, "晴 25 度")
    blocked = g.check("get_weather", args)
    c.check("★★ 相同参数第二次 → **短路**", blocked is not None)
    c.check("★ 短路提示里带**上次结果**（让模型有依据换路子）",
            "晴 25 度" in (blocked or ""), (blocked or "")[:80])
    c.check("★ 短路提示里说明**已调用几次**", "1 次" in (blocked or ""), blocked or "")
    c.check("★ 短路提示里给出**下一步动作**（换工具/改参数/直接作答）",
            "换一个工具" in (blocked or ""), blocked or "")

    c.check("★ 参数不同 → 不拦", g.check("get_weather", {"city": "喀什"}) is None)
    c.check("★ 工具不同 → 不拦", g.check("web_search", args) is None)
    c.check("★ 参数顺序不同但语义相同 → **要拦**（sort_keys 指纹）",
            g.check("get_weather", {"date": "2026-10-18", "city": "阿克苏"}) is not None)

    # ------------------------------------------------------------------
    c.section("第 7 节：★★ 空转回答 —— 必须「全等」，不能「包含」")
    # ------------------------------------------------------------------
    # 真·空转
    for t in ("任务已完成", "无需更多任务", "任务已完成。", "  任务已完成  ", "任务完成！"):
        c.check(f"★ 空转被判出: {t!r}", dispatch.is_empty_response(t))

    # ★★ 这些**看起来像**空转，其实是真回答 —— 绝不能误杀（第一版就是在这里错的）
    for t in ("任务已完成，产量 125 公斤/亩",
              "无需更多任务，但注意南疆要提前滴水",
              "任务已完成；另外图表已生成，见下方",
              "以上是完整分析，包括产量趋势与价格波动。"):
        c.check(f"★★ **不误杀真回答**: {t[:22]!r}…", not dispatch.is_empty_response(t))

    c.check("★ 空串不算空转（那是「没有回答」，由别的机制处理）",
            not dispatch.is_empty_response(""))
    c.check("★ None 不抛异常", not dispatch.is_empty_response(None))
    c.check("★ 超长文本直接判正常（省一次比对）",
            not dispatch.is_empty_response("任务已完成" + "x" * 80))

    # ------------------------------------------------------------------
    c.section("第 8 节：★ 接线是否真的接上了（防「写了没接线」）")
    # ------------------------------------------------------------------
    rag = (ROOT / "core" / "rag_engine.py").read_text(encoding="utf-8")
    c.check("★★ 工具循环里**不再有**静默 `func_args = {}`",
            "except json.JSONDecodeError:\n                    func_args = {}" not in rag)
    c.check("★ 工具循环调用了 `parse_tool_arguments`",
            "parse_tool_arguments" in rag)
    c.check("★ 工具循环用了 `LoopGuard`", "LoopGuard" in rag)
    c.check("★ 工具循环用了 `is_empty_response`", "is_empty_response" in rag)

    tools_init = (ROOT / "core" / "tools" / "__init__.py").read_text(encoding="utf-8")
    c.check("★ 未知工具走 `resolve_tool_name`（不再是裸字符串）",
            "resolve_tool_name" in tools_init)

    mcp = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
    c.check("★★ MCP 侧与原生侧**共用同一份**未知工具提示（消除两份地图）",
            "_dispatch.resolve_tool_name" in mcp)
    c.check("★ MCP 侧不再手写第二份工具清单文案",
            "f\"未知工具: {name}。可用工具: \"" not in mcp)

    print()
    print("=" * 78)
    print(f"工具调度防线守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())