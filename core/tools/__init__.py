# -*- coding: utf-8 -*-
"""工具层统一入口 —— **桌面端与 MCP 服务端共用的唯一实现**。

设计要点
--------
1. **单一真相**：工具契约（`schemas.TOOL_SCHEMAS`）与实现（本包各模块）只有一份。
   桌面端（`core/rag_engine.py`）与 MCP 服务端（`mcp_server.py`）都从这里取，
   因此不存在"两套工具定义各自演化"的漂移风险。
2. **同构映射**：`TOOL_SCHEMAS` 是 OpenAI / DeepSeek Function Calling 格式，
   与 MCP 的 `Tool` **同构**（`function.parameters` 即 `inputSchema`）。
   MCP 侧只需改形、无需重新声明 —— 见 `mcp_server.py` 与 `mcp_adapter.py`。
3. **结构化返回**：`execute_tool_result` 返回 `ToolResult`（文本 + 可选图片），
   使 `plot_trend` 这类复合结果能被两条路径各自消费：桌面端取 `text`
   （内含 `chart_path` 供 `![图表]` 嵌入），MCP 端取 `images` 转 `ImageContent`
   —— **后者不落盘**。

模块依赖是**单向**的：`result` ← 各工具模块 ← `__init__`，无循环导入。
"""
from __future__ import annotations

import logging

from core.tools import capabilities, dispatch
from core.tools.external import get_weather, web_search
from core.tools.local_data import (
    analyze_yield,
    calc_price_volatility,
    query_cotton_stats,
)
from core.tools.market import get_cotton_index
from core.tools.plot import plot_trend
from core.tools.result import ToolResult
from core.tools.schemas import TOOL_SCHEMAS

__all__ = [
    "TOOL_SCHEMAS",
    "TOOL_HANDLERS",
    "ToolResult",
    "capabilities",
    "dispatch",
    "execute_tool",
    "execute_tool_result",
]


#: 本模块**不得使用 `print()`** —— MCP 服务端（stdlib 传输）的 **stdout 承载 JSON-RPC**，
#: 任何写到 stdout 的东西都会污染协议（`tests/smoke_mcp.py` 第 3 节在守这条线）。
#: 需要留痕时一律走 logging（写日志文件，不碰 stdout）。
log = logging.getLogger(__name__)


#: 名称 → 实现。**新增工具只需在此登记 + 在 schemas.py 加契约。**
TOOL_HANDLERS = {
    "get_weather": get_weather,
    "web_search": web_search,
    "query_cotton_stats": query_cotton_stats,
    "calc_price_volatility": calc_price_volatility,
    "plot_trend": plot_trend,
    "analyze_yield": analyze_yield,
    # ★ 当日价格（现货指数 + 期货）：知识库那份 CSV 是打包快照，回答不了"今天"
    "get_cotton_index": get_cotton_index,
}


def execute_tool(name: str, args: dict) -> str:
    """按名字执行工具，返回**文本**结果（保持历史接口不变）。

    需要图片的调用方请用 `execute_tool_result()`。
    """
    return execute_tool_result(name, args).text


def execute_tool_result(name: str, args: dict) -> ToolResult:
    """按名字执行工具，返回结构化结果（文本 + 可选图片）。

    ★ **单一闸门**：本函数是工具的**唯一分发入口** —— 原生路径
    （`rag_engine._execute_tool`）与 MCP（`mcp_server.py`）都经此处，
    因此能力注解的判定**只在这里做一次**，不存在旁路。

    实现既可返回 `ToolResult`（如 `plot_trend`），也可返回裸 `str`
    （其余工具），此处统一包装。
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        # ★ 防线一：先试**别名/归一化**（`websearch` → `web_search`），
        # 再放弃。**刻意不回落某个"默认工具"** —— 本项目的 6 个工具语义不相交，
        # 替模型猜会给出"看起来正常、实际无关"的答案，比报错危险得多。
        # 理由详见 `core/tools/dispatch.py` 的模块注释。
        resolved = dispatch.resolve_tool_name(name, list(TOOL_HANDLERS))
        if not resolved.ok:
            log.warning("未知工具名: %r（已给出候选纠正）", name)
            return ToolResult(text=resolved.message)
        log.info("工具名归一: %r → %r（%s）", name, resolved.canonical, resolved.note)
        name = resolved.canonical
        handler = TOOL_HANDLERS[name]

    # 能力闸门（fail-closed：闸门自身出错也拒绝）
    decision = capabilities.guard(name, args)
    if not decision.allowed:
        # ⚠️ 用 logging 而不是 print：MCP 服务端的 stdout 是 JSON-RPC 协议通道。
        # 拒绝理由仍会**随工具结果返回给模型**（见下），所以信息不会丢。
        log.warning("工具调用被能力闸门拒绝: %s — %s", name, decision.reason)
        return ToolResult(text=decision.as_error_text(name))

    out = handler(args or {})
    return out if isinstance(out, ToolResult) else ToolResult(text=str(out))
