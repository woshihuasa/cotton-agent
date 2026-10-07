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

from core.tools.external import get_weather, web_search
from core.tools.local_data import (
    analyze_yield,
    calc_price_volatility,
    query_cotton_stats,
)
from core.tools.plot import plot_trend
from core.tools.result import ToolResult
from core.tools.schemas import TOOL_SCHEMAS

__all__ = [
    "TOOL_SCHEMAS",
    "TOOL_HANDLERS",
    "ToolResult",
    "execute_tool",
    "execute_tool_result",
]


#: 名称 → 实现。**新增工具只需在此登记 + 在 schemas.py 加契约。**
TOOL_HANDLERS = {
    "get_weather": get_weather,
    "web_search": web_search,
    "query_cotton_stats": query_cotton_stats,
    "calc_price_volatility": calc_price_volatility,
    "plot_trend": plot_trend,
    "analyze_yield": analyze_yield,
}


def execute_tool(name: str, args: dict) -> str:
    """按名字执行工具，返回**文本**结果（保持历史接口不变）。

    需要图片的调用方请用 `execute_tool_result()`。
    """
    return execute_tool_result(name, args).text


def execute_tool_result(name: str, args: dict) -> ToolResult:
    """按名字执行工具，返回结构化结果（文本 + 可选图片）。

    实现既可返回 `ToolResult`（如 `plot_trend`），也可返回裸 `str`
    （其余工具），此处统一包装。
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return ToolResult(text=f"工具未找到: {name}")
    out = handler(args or {})
    return out if isinstance(out, ToolResult) else ToolResult(text=str(out))
