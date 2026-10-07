# -*- coding: utf-8 -*-
"""MCP 改形层 —— 在"我们自己的工具契约"与"MCP 协议类型"之间做**纯函数**转换。

为什么单独成层
--------------
MCP 的 `Tool` 与 OpenAI / DeepSeek 的 Function Calling 定义**是同构的**：

    OpenAI:  {"type": "function", "function": {"name", "description", "parameters"}}
    MCP:                                          name    description   inputSchema

三要素一一对应，只有**外层包装与字段名**不同。因此这里**不做任何语义改写**，
只做机械改形 —— 这保证了"工具契约只有一份"（`core/tools/schemas.py`），
不会出现 MCP 侧另立一套、两边各自演化的漂移。

放在纯函数层还有个好处：不需要起 MCP 服务就能**单元测试**改形是否正确
（见 `tests/smoke_mcp.py`）。
"""
from __future__ import annotations

import base64

from mcp_types import ImageContent, TextContent

from core.tools.result import ToolResult

#: MCP 调用时给工具补的**内部开关**（不在对外 schema 里，LLM 不会传）。
#: `plot_trend` 默认会落盘以取得 `chart_path` 供桌面端嵌图；走 MCP 时图片是
#: 内联返回的，宿主不需要也无法访问本机文件，故关掉落盘、避免在宿主机器上留垃圾。
MCP_INTERNAL_ARGS: dict[str, dict] = {
    "plot_trend": {"_materialize": False},
}


def to_mcp_tool(schema: dict) -> dict:
    """OpenAI Function Calling 定义 → MCP `Tool` 的构造参数。

    Args:
        schema: `core.tools.schemas.TOOL_SCHEMAS` 中的一项。

    Returns:
        可直接 `Tool(**...)` 的 dict：`{"name", "description", "inputSchema"}`。

    Raises:
        KeyError: schema 结构不合法（缺 function / name / description / parameters）。
    """
    fn = schema["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "inputSchema": fn["parameters"],
    }


def with_mcp_overrides(name: str, args: dict) -> dict:
    """给一次 MCP 工具调用补上内部开关（不修改传入的 dict）。"""
    extra = MCP_INTERNAL_ARGS.get(name)
    return {**args, **extra} if extra else dict(args)


def to_content_blocks(result: ToolResult) -> list:
    """`ToolResult` → MCP content blocks。

    文本块始终返回；图片块按需追加（base64 内联，**不引用任何本地路径** ——
    这正是 `plot_trend` 改造要解决的问题）。
    """
    blocks: list = [TextContent(type="text", text=result.text)]
    for data, mime in result.images:
        blocks.append(ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mime_type=mime or "image/png",
        ))
    return blocks
