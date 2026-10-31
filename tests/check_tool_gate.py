# -*- coding: utf-8 -*-
"""工具能力闸门守卫 —— 注解 · fail-closed · 不旁路 · 不破坏既有守卫。

【定位】`docs/AGENT_RUNTIME.md` 步 7 的验收。四条硬要求：

  ① **双向完整性**：注解表的键集必须与工具集完全相等（防漏声明 / 防孤儿声明）
  ② **缺省即保守**：漏声明的工具落到"更严"而不是"更松"（失败方向安全）
  ③ **fail-closed**：闸门自身抛异常 → 一律拒绝（「守卫抛异常时若放行，等于没守卫」）
  ④ **不破坏既有守卫**：注解**不得进 schema** —— 否则 `smoke_mcp.py` 的
     「inputSchema 与本地契约逐字段一致」当场失败

【零 LLM / 零 API】纯本地判定。

【用法】python tests/check_tool_gate.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.tools import TOOL_HANDLERS, TOOL_SCHEMAS, capabilities   # noqa: E402
from core.tools import execute_tool, execute_tool_result           # noqa: E402


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


def main() -> int:
    c = Checker()
    schema_names = {t["function"]["name"] for t in TOOL_SCHEMAS}

    # ------------------------------------------------------------------
    c.section("第 1 节：★ 双向完整性（防漏声明 / 防孤儿声明）")
    # ------------------------------------------------------------------
    c.check("注解表键集 == 实现表键集",
            set(capabilities.CAPABILITIES) == set(TOOL_HANDLERS),
            f"注解多 {sorted(set(capabilities.CAPABILITIES) - set(TOOL_HANDLERS))} / "
            f"少 {sorted(set(TOOL_HANDLERS) - set(capabilities.CAPABILITIES))}")
    c.check("注解表键集 == schema 工具名集",
            set(capabilities.CAPABILITIES) == schema_names,
            f"{sorted(capabilities.CAPABILITIES)} vs {sorted(schema_names)}")
    c.check("工具数确为 7（新增工具必须同时改这一行，防「悄悄多出来一个」）",
            len(schema_names) == 7, str(len(schema_names)))
    c.check("known_tool_names() 与 schema 一致",
            capabilities.known_tool_names() == schema_names)

    # ------------------------------------------------------------------
    c.section("第 2 节：★ 缺省即保守（失败方向安全）")
    # ------------------------------------------------------------------
    d = capabilities.capability_of("这个工具根本不存在")
    c.check("未声明的工具 → **非只读**", d.read_only is False)
    c.check("未声明的工具 → **可破坏**", d.destructive is True)
    c.check("未声明的工具 → **开放世界**", d.open_world is True)
    c.check("未声明的工具 → **非幂等**", d.idempotent is False)
    c.check("保守缺省与显式声明可区分",
            capabilities.capability_of("get_weather").destructive is False)

    c.check("plot_trend 被标为**会写盘**（非只读 + 可破坏）",
            capabilities.capability_of("plot_trend").destructive
            and not capabilities.capability_of("plot_trend").read_only)
    c.check("web_search 被标为开放世界",
            capabilities.capability_of("web_search").open_world is True)
    c.check("本地统计工具**不**是开放世界",
            capabilities.capability_of("query_cotton_stats").open_world is False)

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 注解**不得进 schema**（保护 smoke_mcp 的既有断言）")
    # ------------------------------------------------------------------
    allowed_keys = {"type", "function"}
    c.check("每个工具定义只有 type / function 两个顶层键",
            all(set(t) <= allowed_keys for t in TOOL_SCHEMAS),
            str([sorted(t) for t in TOOL_SCHEMAS][:2]))
    c.check("function 内只有 name / description / parameters",
            all(set(t["function"]) <= {"name", "description", "parameters"}
                for t in TOOL_SCHEMAS),
            str([sorted(t["function"]) for t in TOOL_SCHEMAS][:2]))
    blob = json.dumps(TOOL_SCHEMAS, ensure_ascii=False)
    for kw in ("readOnly", "read_only", "destructive", "openWorld", "open_world",
               "idempotent"):
        c.check(f"schema 里**不含**注解字段 {kw!r}", kw not in blob)

    # ------------------------------------------------------------------
    c.section("第 4 节：★ fail-closed（闸门自己坏了也必须拒绝）")
    # ------------------------------------------------------------------
    original = capabilities.evaluate

    def _boom(*a, **k):
        raise RuntimeError("模拟闸门内部故障")

    capabilities.evaluate = _boom                      # type: ignore[assignment]
    try:
        dec = capabilities.guard("get_weather", {})
    finally:
        capabilities.evaluate = original               # type: ignore[assignment]
    c.check("★ 闸门抛异常 → **拒绝**（不是放行）", dec.allowed is False, str(dec))
    c.check("拒绝理由说明了是内部错误（可诊断）",
            "fail-closed" in dec.reason and "RuntimeError" in dec.reason, dec.reason)
    c.check("闸门恢复后正常放行（证明上面是注入的故障）",
            capabilities.guard("get_weather", {}).allowed)

    # ------------------------------------------------------------------
    c.section("第 5 节：策略 —— Web 模式禁止会写盘的工具")
    # ------------------------------------------------------------------
    try:
        capabilities.set_mode(owns_user_data=True)          # 桌面端
        c.check("桌面端：plot_trend **放行**",
                capabilities.guard("plot_trend", {}).allowed)
        c.check("桌面端：只读工具放行",
                capabilities.guard("query_cotton_stats", {}).allowed)

        capabilities.set_mode(owns_user_data=False)         # Web 访客
        d_web = capabilities.guard("plot_trend", {})
        c.check("★ Web 模式：plot_trend **被拒**", d_web.allowed is False, str(d_web))
        c.check("拒绝理由说清了**为什么**（写盘 + 不拥有用户数据）",
                "写入磁盘" in d_web.reason and "不拥有用户数据" in d_web.reason,
                d_web.reason)
        c.check("Web 模式：只读工具**仍放行**（不是一刀切）",
                capabilities.guard("query_cotton_stats", {}).allowed)
        c.check("Web 模式：get_weather 放行（只读）",
                capabilities.guard("get_weather", {}).allowed)
    finally:
        capabilities.set_mode(owns_user_data=True)          # 恢复，避免影响后续断言

    # ------------------------------------------------------------------
    c.section("第 6 节：★ 不旁路 —— 两条入口都经闸门")
    # ------------------------------------------------------------------
    try:
        capabilities.set_mode(owns_user_data=False)
        via_result = execute_tool_result("plot_trend", {"metric": "price"})
        via_tool = execute_tool("plot_trend", {"metric": "price"})
        c.check("★ execute_tool_result（MCP 走这条）被闸门拦住",
                "能力闸门拒绝" in via_result.text, via_result.text[:80])
        c.check("★ execute_tool（原生路径走这条）同样被拦住",
                "能力闸门拒绝" in via_tool, via_tool[:80])
        c.check("被拦时**工具实现根本没跑**（返回值不是图表 JSON）",
                "chart_path" not in via_result.text, via_result.text[:80])
    finally:
        capabilities.set_mode(owns_user_data=True)

    c.check("不存在的工具仍走原路径（不受闸门影响）",
            "工具未找到" in execute_tool_result("no_such_tool", {}).text)

    # ------------------------------------------------------------------
    c.section("第 7 节：拒绝文本的形态（与结晶档门禁自然衔接）")
    # ------------------------------------------------------------------
    txt = capabilities.GateDecision(False, "原因ABC").as_error_text("plot_trend")
    parsed = json.loads(txt)
    c.check("拒绝文本是**合法 JSON**（可被工具链解析）", isinstance(parsed, dict))
    c.check("★ 含 error 字段 → `_tool_result_ok()` 会判为**失败**，该轨迹不进结晶档",
            bool(parsed.get("error")))
    c.check("带 tool 名（可定位到具体工具）", parsed.get("tool") == "plot_trend")
    c.check("带 hint（**可操作**，不是只说「不行」）", bool(parsed.get("hint")))

    # ------------------------------------------------------------------
    c.section("第 8 节：模式传递（contextvar）")
    # ------------------------------------------------------------------
    c.check("默认模式为 True（桌面端全权）",
            capabilities.current_mode() is True)
    capabilities.set_mode(False)
    c.check("set_mode 生效", capabilities.current_mode() is False)
    capabilities.set_mode(True)
    c.check("可恢复", capabilities.current_mode() is True)
    c.check("guard 允许显式覆盖模式（便于单测）",
            capabilities.guard("plot_trend", {}, owns_user_data=False).allowed is False)

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"工具能力闸门守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())