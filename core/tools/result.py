# -*- coding: utf-8 -*-
"""工具结果类型。

单独成模块是为了**打破循环导入**：`core.tools.plot` 需要返回 `ToolResult`，
而 `core.tools.__init__` 又要导入 `plot`。把类型放在这里，两边都从本模块取，
依赖图就是单向的：

    result.py  ←  plot.py
        ↑
    __init__.py
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolResult:
    """工具执行结果。

    Attributes:
        text: 给 LLM 阅读的文本结果（JSON 字符串或自然语言），即历史行为。
        images: 附带图片，元素为 `(png_bytes, mime_type)`。
            桌面端忽略它（改用 text 里的 `chart_path` 嵌图）；
            MCP 端把它转成 `ImageContent` 随结果返回 —— **无需落盘**。
    """

    text: str
    images: list[tuple[bytes, str]] = field(default_factory=list)

    def as_text(self) -> str:
        """仅取文本（供只关心文字的调用方，如 Function Calling 路径）。"""
        return self.text
