# -*- coding: utf-8 -*-
"""分级网关守卫 —— 省钱的判据 + **绝不误降级**的安全线。

【定位】`docs/AGENT_RUNTIME.md` 步 5 的验收。

        本守卫的重点**不是**"L0 判得准不准"，而是**第 3 节那条反向断言**：
        「含农事实体或工具触发词的问句**绝不**被判为 L0」。

        理由见 `core/query_router.py` 的模块注释：分级网关的真正风险是**误降级**，
        而那种错误**用户无从察觉**。省下的钱不该以答错为代价。

【零 LLM / 零 API】纯规则判定。

【用法】python tests/check_query_router.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.query_router import (                       # noqa: E402
    L0,
    L1,
    L2,
    classify,
    describe,
    has_farm_entity,
    matched_tool_kinds,
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


#: 明确该走 L0 的寒暄/身份类
GREETINGS = ["你好", "您好", "在吗", "你是谁", "你能做什么", "谢谢", "再见", "hello"]

#: ★ 安全线：这些**一个都不许**被判成 L0
MUST_NOT_BE_L0 = [
    "今天阿拉尔适合打药吗", "棉花苗期需要怎么施肥", "预测一下2027年阿克苏的棉花产量",
    "画一下棉花价格的走势图", "最近棉花价格波动大吗", "各产区的产量波动风险怎么样",
    "你好，棉花价格怎么样", "谢谢，那打药的时间呢", "阿克苏的棉田有多少亩",
    "棉铃虫怎么防治", "南疆的补贴政策", "帮我搜一下最新的棉花新闻",
]


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：L0 —— 寒暄白名单准入")
    # ------------------------------------------------------------------
    for q in GREETINGS:
        d = classify(q)
        c.check(f"L0: {q!r}", d.level == L0, f"{d.level} / {d.reason}")

    d0 = classify("你好")
    c.check("L0 三个跳过标志**全为真**",
            d0.skip_retrieval and d0.skip_tools and d0.skip_memory,
            f"{d0.skip_retrieval}/{d0.skip_tools}/{d0.skip_memory}")
    c.check("L0 的 reason 非空（可诊断）", bool(d0.reason))

    # ------------------------------------------------------------------
    c.section("第 2 节：L1 —— 只命中一类信号")
    # ------------------------------------------------------------------
    d_tool = classify("画一下棉花价格的走势图")
    c.check("工具型 → L1", d_tool.level == L1, f"{d_tool.level} / {d_tool.reason}")
    c.check("工具型**跳过检索**、不跳过工具",
            d_tool.skip_retrieval and not d_tool.skip_tools,
            f"{d_tool.skip_retrieval}/{d_tool.skip_tools}")

    d_ret = classify("棉花苗期需要怎么施肥")
    c.check("检索型 → L1", d_ret.level == L1, f"{d_ret.level} / {d_ret.reason}")
    c.check("检索型**跳过工具**、不跳过检索",
            d_ret.skip_tools and not d_ret.skip_retrieval,
            f"{d_ret.skip_tools}/{d_ret.skip_retrieval}")

    c.check("L1 不跳过记忆（保守：记忆用于个性化，只省检索或工具）",
            not d_tool.skip_memory and not d_ret.skip_memory)

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 安全线 —— 绝不误降级")
    # ------------------------------------------------------------------
    for q in MUST_NOT_BE_L0:
        d = classify(q)
        c.check(f"★ 不判 L0: {q[:22]!r}", d.level != L0, f"竟判 {d.level} / {d.reason}")

    # 更硬的一条：凡含农事实体的问句，一律不得 L0（逐条独立断言）
    farm_qs = [q for q in MUST_NOT_BE_L0 if has_farm_entity(q)]
    c.check(f"抽出 {len(farm_qs)} 条含农事实体的问句", len(farm_qs) >= 8, str(len(farm_qs)))
    c.check("★★ 含农事实体 → **无一判 L0**",
            all(classify(q).level != L0 for q in farm_qs),
            str([q for q in farm_qs if classify(q).level == L0]))

    tool_qs = [q for q in MUST_NOT_BE_L0 if matched_tool_kinds(q)]
    c.check(f"抽出 {len(tool_qs)} 条含工具触发词的问句", len(tool_qs) >= 4, str(len(tool_qs)))
    c.check("★★ 含工具触发词 + 寒暄前缀 → 仍不判 L0",
            all(classify(q).level != L0 for q in tool_qs),
            str([q for q in tool_qs if classify(q).level == L0]))

    # ------------------------------------------------------------------
    c.section("第 4 节：默认 L2（没把握就付全价）")
    # ------------------------------------------------------------------
    cases = [
        ("", "空问句"),
        ("   ", "纯空白"),
        ("你好，今天阿拉尔适合打药吗", "寒暄 + 工具 + 实体"),
        ("棉花价格怎么样，未来会涨吗", "工具 + 检索混合"),
        ("asdfghjkl", "无意义输入"),
        ("今天天气怎么样，顺便说说施肥要点", "工具 + 检索"),
    ]
    for q, why in cases:
        d = classify(q)
        c.check(f"L2: {why}", d.level == L2, f"{d.level} / {d.reason}")

    c.check("L2 什么都不跳过（与改动前逐字一致）",
            not any((classify("棉花价格怎么样，未来会涨吗").skip_retrieval,
                     classify("棉花价格怎么样，未来会涨吗").skip_tools,
                     classify("棉花价格怎么样，未来会涨吗").skip_memory)))

    c.check("超长寒暄 → L2（长度闸）",
            classify("你好" + "啊" * 40).level == L2,
            classify("你好" + "啊" * 40).reason)

    # ------------------------------------------------------------------
    c.section("第 5 节：地区名单复用单一真相（未另建一份）")
    # ------------------------------------------------------------------
    c.check("能从 rag_engine 取到地区清单（延迟导入生效）", has_farm_entity("阿拉尔"))
    c.check("南疆/北疆 等区域词也算农事实体", has_farm_entity("南疆的棉花"))
    c.check("无实体问句不误判", not has_farm_entity("今天心情不错"))
    c.check("工具分类可多类同命中",
            len(matched_tool_kinds("画个价格走势图并预测产量")) >= 2,
            str(matched_tool_kinds("画个价格走势图并预测产量")))

    # ------------------------------------------------------------------
    c.section("第 6 节：可诊断性")
    # ------------------------------------------------------------------
    c.check("describe 含档位与三个跳过标志",
            all(k in describe(classify("你好")) for k in ("路由", "跳过", "检索", "工具", "记忆")),
            describe(classify("你好")))
    c.check("判定的 hits 是元组（不可变、可比较）",
            isinstance(classify("画个走势图").hits, tuple))

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"分级网关守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())