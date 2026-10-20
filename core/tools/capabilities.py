# -*- coding: utf-8 -*-
"""工具能力声明与单一闸门 —— 没有权限系统时，用「注解 + 单一 fail-closed 闸门」替代。

【为什么需要】6 个原生工具 + MCP 暴露的工具**共用一套实现**，但此前**没有任何能力声明**：
"这个工具会写盘""那个工具会联网"只存在于人的脑子里与文档里。
于是"Web 访客场景不该往宿主磁盘写图"这类约束**无处可依**。

【★ 注解放在平行表，绝不写进 schema】三条理由，第一条是硬的：
  ① `tests/smoke_mcp.py` 断言「MCP 返回的 `inputSchema` 与本地契约**逐字段一致**」——
     往 `TOOL_SCHEMAS` 里塞注解会**直接打破这条既有守卫**；
  ② 注解是**本地治理信息**，对模型无用，只会白占 token；
  ③ 平行表可以**双向校验**（键集必须与 `TOOL_HANDLERS` 相等，防漏声明 / 防孤儿），
     混进 schema 就无法自检。

【★ 缺省即保守】（照 pi 的 MCP 语义）未声明 = **非只读 · 可能破坏 · 开放世界**。
漏声明一个工具，结果是"更严"而不是"更松" —— **失败方向安全**。

【★ fail-closed】闸门自身抛异常 → **一律拒绝**，不是放行。
理由：**「守卫抛异常时若放行，等于没守卫」**。

【★ 不旁路】闸门设在 `core/tools/__init__.py:execute_tool_result()` ——
那是**唯一分发入口**，原生路径（`rag_engine._execute_tool`）与 MCP（`mcp_server.py`）
都经此处，因此**一处改动全覆盖**（pi #33）。

【运行模式如何传进来】用 `contextvars` 而不是改签名 —— 因为 `_execute_tool` 被
`tests/eval_numeric.py` 以**未绑定静态方法**方式调用（`RAGEngine._execute_tool(...)`），
改签名会打破它。默认 True（桌面端全权）。
"""
from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass

from core.tools.schemas import TOOL_SCHEMAS


@dataclass(frozen=True)
class ToolCapability:
    """一个工具的能力声明（**本地治理信息，不进 schema**）。"""

    read_only: bool = False        # 只读（不改变外部世界）
    destructive: bool = False      # 可能造成不可逆改变（含写盘）
    open_world: bool = True        # 会触达外部世界（网络）
    idempotent: bool = True        # 重复调用结果一致

    def describe(self) -> str:
        return (f"{'只读' if self.read_only else '非只读'}"
                f"{'·可破坏' if self.destructive else ''}"
                f"{'·开放世界' if self.open_world else ''}")


#: ★ 缺省即保守：**未声明 = 非只读 · 可能破坏 · 开放世界**。
CONSERVATIVE_DEFAULT = ToolCapability(
    read_only=False, destructive=True, open_world=True, idempotent=False,
)

#: 名称 → 能力。**必须与 `TOOL_HANDLERS` 的键集完全一致**（守卫双向断言）。
CAPABILITIES: dict[str, ToolCapability] = {
    "get_weather": ToolCapability(read_only=True, destructive=False, open_world=True),
    "web_search": ToolCapability(read_only=True, destructive=False,
                                 open_world=True, idempotent=False),
    "query_cotton_stats": ToolCapability(read_only=True, destructive=False,
                                         open_world=False),
    "calc_price_volatility": ToolCapability(read_only=True, destructive=False,
                                            open_world=False),
    "analyze_yield": ToolCapability(read_only=True, destructive=False, open_world=False),
    # plot_trend 会把图表**写到磁盘**（chart_path），因此不是只读、且可破坏
    "plot_trend": ToolCapability(read_only=False, destructive=True, open_world=False),
}


def capability_of(name: str) -> ToolCapability:
    """取能力声明；**未声明返回保守缺省**（不是宽松缺省）。"""
    return CAPABILITIES.get(name, CONSERVATIVE_DEFAULT)


# ---- 运行模式（contextvar：不改任何调用签名即可传递）----

_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "owns_user_data", default=True,   # 默认桌面端：拥有数据、全权
)


def set_mode(owns_user_data: bool) -> None:
    """设置本次执行上下文的运行模式（幂等；每次工具循环开头调用一次即可）。"""
    _MODE.set(bool(owns_user_data))


def current_mode() -> bool:
    return _MODE.get()


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""

    def as_error_text(self, tool: str = "") -> str:
        """拒绝时**喂回模型**的文本 —— 必须让模型能据以自纠，而不是只说「不行」。

        故意做成 JSON 且带 `error` 字段：这样 `rag_engine._tool_result_ok()`
        会把它判为**失败**，该轮轨迹因此**不会进结晶档** ——
        两套机制（闸门 / 结晶档门禁）自然衔接，无需额外约定。
        """
        return json.dumps(
            {"error": "工具调用被能力闸门拒绝", "tool": tool, "reason": self.reason,
             "hint": "请换一个不违反该约束的做法，或直接说明该操作在当前模式下不可用。"},
            ensure_ascii=False,
        )


def evaluate(name: str, args: dict, *, owns_user_data: bool) -> GateDecision:
    """策略判定（纯函数，便于单测）。**首批策略刻意最小。**"""
    cap = capability_of(name)
    if not owns_user_data and cap.destructive:
        return GateDecision(
            False,
            f"工具 {name} 会写入磁盘/造成不可逆改变（{cap.describe()}），"
            f"而当前会话不拥有用户数据（Web 访客场景）—— 不应改动宿主环境。",
        )
    return GateDecision(True, "")


def guard(name: str, args: dict, *, owns_user_data: bool | None = None) -> GateDecision:
    """闸门入口：**任何内部异常都转为拒绝**（fail-closed）。

    这是"守卫抛异常时若放行，等于没守卫"的直接实现 ——
    宁可拒绝一次合法调用（用户看得见、可重试），也不放行一次越权调用（静默、不可逆）。
    """
    try:
        mode = current_mode() if owns_user_data is None else bool(owns_user_data)
        return evaluate(name, args, owns_user_data=mode)
    except Exception as e:                                    # noqa: BLE001
        return GateDecision(
            False,
            f"能力闸门内部错误，按 fail-closed 拒绝: {type(e).__name__}: {e}",
        )


def known_tool_names() -> set[str]:
    """现有工具名集合（供守卫做双向完整性断言）。"""
    return {t["function"]["name"] for t in TOOL_SCHEMAS}