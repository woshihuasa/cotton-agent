#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""棉花智能问答助手 · MCP Server（stdio）

把 6 个本地工具通过 **Model Context Protocol** 暴露给任意支持 MCP 的宿主
（Claude Desktop / Cursor / Cline 等）。

设计取舍
--------
**为什么是 stdio 而不是 HTTP**：数据（`data/新疆_数据统计/*.csv`）在本机、
服务无状态、宿主与进程同机 —— stdio 零部署、零鉴权、零端口，且与项目
"不做公网服务"的既定取向一致（见 ROADMAP 决策记录 11：F1b 判死）。

**为什么继承 MCPServer 而不是用 @mcp.tool() 装饰器**：
    `add_tool()` 从**类型注解**推导 inputSchema。若顺着它走，就等于把 6 个工具
    **在 MCP 侧重新声明一遍**，从而与 `core/tools/schemas.py`（桌面端 Function
    Calling 用的那份、经 `eval_tools` 验证 97.8%）形成**两份真相**，改一处忘一处
    必然漂移。
    `MCPServer.list_tools()` / `call_tool()` 是**公开方法**（官方留的扩展点），
    覆写它们即可让工具定义直接来自同一份契约，同时白拿 SDK 的协议握手、
    stdio 传输与 JSON-RPC 编排。

启动
----
    python mcp_server.py                     # 由宿主以 stdio 子进程方式拉起

宿主配置示例（Claude Desktop 的 claude_desktop_config.json）：
    {
      "mcpServers": {
        "cotton-agent": {
          "command": "E:\\\\cotton_agent\\\\.venv\\\\Scripts\\\\python.exe",
          "args": ["E:\\\\cotton_agent\\\\mcp_server.py"],
          "env": {
            "AMAP_API_KEY": "...",
            "TAVILY_API_KEY": "..."
          }
        }
      }
    }
"""
from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)                      # 工具依赖相对路径读取 data/ 下的 CSV
sys.path.insert(0, str(ROOT))


def _redirect_print_to_stderr() -> None:
    """把 `print()` 改道到 **stderr**，保护 stdout 协议通道。

    背景：stdio 传输用 **stdout 承载 JSON-RPC 报文**。工具层里任何一句
    `print()`（例如错误分支的诊断输出）都会往协议流里注入非 JSON 文本，
    导致宿主报"解析失败"甚至直接断开 —— 而且**只在错误路径触发**，
    冒烟测试很难覆盖到。

    这里不改 `sys.stdout` 本身（SDK 仍从它取协议通道），只替换
    `builtins.print` 的默认输出目标，因此对协议零影响。
    属于"纵深防御"：即便将来有人在被复用的工具模块里写漏了一句 print，
    也不会污染报文。
    """
    # 必须先抓住**原始** print：替换之后再调用 builtins.print 就是调自己（无限递归）
    _original_print = builtins.print

    def _print(*args, **kwargs):            # type: ignore[no-untyped-def]
        kwargs.setdefault("file", sys.stderr)
        _original_print(*args, **kwargs)

    builtins.print = _print


_redirect_print_to_stderr()

from mcp.server.mcpserver import MCPServer                    # noqa: E402
from mcp_types import CallToolResult, Tool as MCPTool         # noqa: E402

from core.tools import TOOL_HANDLERS, TOOL_SCHEMAS, execute_tool_result  # noqa: E402
from core.tools.mcp_adapter import (                          # noqa: E402
    to_content_blocks,
    to_mcp_tool,
    with_mcp_overrides,
)

VERSION = (ROOT / "build_version.txt").read_text(encoding="utf-8").strip() \
    if (ROOT / "build_version.txt").exists() else "dev"

INSTRUCTIONS = """本服务提供新疆棉花种植相关的实时与统计数据工具。

工具选择建议：
- 天气 / 能否打药        → get_weather
- 最新政策、新闻、行情   → web_search
- 产量 / 面积 / 亩产     → query_cotton_stats
- 价格水平、涨跌幅、分位 → calc_price_volatility
- 趋势折线图             → plot_trend（图片直接内联返回，无需文件路径）
- 产量预测、产区风险分级 → analyze_yield

注意：数据范围为中国新疆，粒度为地区/年份；超出范围请如实说明。"""


class CottonMCPServer(MCPServer):
    """只接管"工具从哪来"，其余全部交给 SDK。

    覆写两个**公开扩展点**：

    * `list_tools`  —— 工具定义直接由 `TOOL_SCHEMAS` 改形得到（单一真相）
    * `call_tool`   —— 分发到 `core.tools`，并把 `ToolResult` 转成 MCP content
    """

    async def list_tools(self) -> list[MCPTool]:
        """把同一份工具契约改形为 MCP Tool 列表。"""
        return [MCPTool(**to_mcp_tool(s)) for s in TOOL_SCHEMAS]

    async def call_tool(self, name: str, arguments: dict, context=None) -> CallToolResult:
        """执行工具并返回 MCP 结果（文本 + 可选内联图片）。

        未知工具不抛异常，而是回一个 `is_error=True` 的结果 ——
        让宿主模型能读到错误原因并自行纠正，比直接断开连接更友好。
        """
        if name not in TOOL_HANDLERS:
            return CallToolResult(
                content=[{"type": "text",
                          "text": f"未知工具: {name}。可用工具: "
                                  f"{', '.join(sorted(TOOL_HANDLERS))}"}],
                is_error=True,
            )

        args = with_mcp_overrides(name, arguments or {})
        result = execute_tool_result(name, args)
        return CallToolResult(content=to_content_blocks(result))


def build_server() -> CottonMCPServer:
    """构造服务实例（供入口与测试共用）。"""
    return CottonMCPServer(
        name="cotton-agent",
        title="棉花智能问答助手工具集",
        description="新疆棉花种植的天气、统计、价格、绘图与产量分析工具",
        instructions=INSTRUCTIONS,
        version=VERSION or "dev",
    )


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
