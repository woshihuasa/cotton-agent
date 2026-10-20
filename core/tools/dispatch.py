# -*- coding: utf-8 -*-
"""工具调度三层防线 —— 让"工具调错"从**静默失效**变成**可纠正**。

【问题的真实形态】模型（LLM）在 Function Calling 里会犯三类错：

    · **编错工具名** —— 调 `websearch` 而实际叫 `web_search`；调 `plot` 而实际叫 `plot_trend`
    · **参数不是合法 JSON** —— 输出 `{city: 阿克苏}`（少引号）、或带 ```json 围栏、或截断
    · **原地打转** —— 用**完全相同的参数**反复调同一个工具，每轮都在烧钱

    这三类**都不会让程序崩**，但都会让回答变差。而原来的实现是**静默降级**：
    参数解析失败 → `func_args = {}`（既不记日志也不回灌，模型永远不知道自己错了）。

【三层防线】

    ┌─ ① 名字解析 ─────────────────────────────────────────┐
    │ 别名归一（`websearch` → `web_search`）               │
    │ 否则给出**带候选的纠正提示**（`你是不是想调 X`）        │
    └──────────────────────────────────────────────────────┘
    ┌─ ② 参数多级降级 ─────────────────────────────────────┐
    │ 标准 JSON → 剥 ``` 围栏 → 抽第一个 {...} →            │
    │ `ast.literal_eval`（Python 字面量 / 单引号）           │
    │ 全失败时**返回失败**（而不是空 dict），由调用方回灌给模型 │
    └──────────────────────────────────────────────────────┘
    ┌─ ③ 循环防护 ─────────────────────────────────────────┐
    │ 相同 (工具, 参数) 超过 N 次 → **短路返回上次结果** +    │
    │ 明确告诉模型"别再调了"                                │
    └──────────────────────────────────────────────────────┘

【★ 一条刻意的设计判断：**不做"默认工具回落"**】

    AgentGPT 的做法是"工具名匹配不到就调 Search"。**本模块不那样做**，原因：

    它的工具是**可互换的**（都是搜索）；而本项目的 6 个工具**语义完全不相交** ——
    把 `plot_trend` 拼错后静默去跑 `web_search`，会**给出一个看起来正常、实际无关的答案**，
    比直接报错**危险得多**（这与 `04` 号 §8.7 那个"静默失效"是同一类风险）。

    所以这里的选择是：**别名归一（只处理语义等价的名字）+ 纠正提示（把选择权还给模型）**，
    而不是替它猜。**猜错了没人知道；报错了模型会自己改。**

【依赖】只用标准库。**不得 import `core.tools`** —— 那是本模块的调用方，会循环导入。
"""
from __future__ import annotations

import ast
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 防线一：工具名解析
# ---------------------------------------------------------------------------

#: 语义等价的别名 → 规范名。
#: ★ 只收**确定等价**的写法（大小写、下划线、口语简称），**不做语义猜测**。
TOOL_ALIASES: dict[str, str] = {
    # web_search
    "search": "web_search",
    "websearch": "web_search",
    "internet_search": "web_search",
    "google": "web_search",
    # get_weather
    "weather": "get_weather",
    "getweather": "get_weather",
    "weather_query": "get_weather",
    # query_cotton_stats
    "stats": "query_cotton_stats",
    "cotton_stats": "query_cotton_stats",
    "query_stats": "query_cotton_stats",
    "get_stats": "query_cotton_stats",
    "statistics": "query_cotton_stats",
    # plot_trend
    "plot": "plot_trend",
    "chart": "plot_trend",
    "draw": "plot_trend",
    "trend": "plot_trend",
    "plot_chart": "plot_trend",
    # calc_price_volatility
    "volatility": "calc_price_volatility",
    "price": "calc_price_volatility",
    "price_volatility": "calc_price_volatility",
    # analyze_yield
    "analyze": "analyze_yield",
    "yield_predict": "analyze_yield",
    "predict": "analyze_yield",
    "forecast": "analyze_yield",
    "risk": "analyze_yield",
}

_NORM_RE = re.compile(r"[^a-z0-9]")


def normalize_name(name: str) -> str:
    """归一化工具名：转小写，去掉下划线/横线/空格等一切非字母数字。

    `web_search` / `WebSearch` / `web-search` / `web search` → 全是 `websearch`
    """
    return _NORM_RE.sub("", str(name or "").lower())


#: ★ 别名表的**归一化索引**。
#:
#: 【为什么需要它】上面的 `TOOL_ALIASES` 为了可读，键写成 `cotton_stats` / `price_volatility`
#: 这种带下划线的形式；而查找时用的是**归一化后的**名字（`cottonstats`）。
#: 第一版直接 `TOOL_ALIASES.get(norm_raw)`，于是**所有带下划线的别名全部失效**
#: （守卫实测抓到的：`cotton_stats` 解析不出来）。
#:
#: 现在两边都归一化：**键归一 → 值用规范名**。表可读，查找可靠。
_ALIAS_INDEX: dict[str, str] = {normalize_name(k): v for k, v in TOOL_ALIASES.items()}


@dataclass(frozen=True)
class NameResolution:
    """工具名解析结果。"""

    ok: bool
    canonical: str = ""      #: 规范化后的工具名（`ok=True` 时有效）
    note: str = ""           #: 说明（别名归一时会写明"由 X 归一而来"）
    message: str = ""        #: `ok=False` 时给模型的纠正提示

    def __bool__(self) -> bool:                              # noqa: D105
        return self.ok


def resolve_tool_name(name: str, available: list[str] | tuple[str, ...]) -> NameResolution:
    """把模型给的工具名解析成规范名。

    顺序：**精确命中 → 归一化命中 → 别名表 → 失败（带候选）**。
    """
    raw = str(name or "").strip()
    avail = list(available)
    if not raw:
        return NameResolution(False, message=unknown_tool_message(raw, avail))

    # ① 精确命中
    if raw in avail:
        return NameResolution(True, raw)

    # ② 归一化后相同（大小写 / 下划线差异）
    norm_raw = normalize_name(raw)
    for a in avail:
        if normalize_name(a) == norm_raw:
            return NameResolution(True, a, note=f"由 {raw!r} 归一而来")

    # ③ 别名表（**查归一化索引**，不是查原表 —— 原表的键带下划线）
    alias = _ALIAS_INDEX.get(norm_raw)
    if alias and alias in avail:
        return NameResolution(True, alias, note=f"由别名 {raw!r} 归一而来")

    # ④ 失败
    return NameResolution(False, message=unknown_tool_message(raw, avail))


def suggest_tool_names(name: str, available: list[str] | tuple[str, ...],
                       limit: int = 3) -> list[str]:
    """给"最接近的几个工具名"（用于纠正提示）。"""
    norm = normalize_name(name)
    # 先比归一化名（这样 `plot` vs `plot_trend`、`websearch` vs `web_search` 都能匹配上）
    normalized = {a: normalize_name(a) for a in available}
    # difflib 需要字符串列表；用归一化名匹配，再映射回原名
    cand = difflib.get_close_matches(
        norm, [normalized[a] for a in available], n=limit, cutoff=0.5)
    out: list[str] = []
    for c in cand:
        for a, na in normalized.items():
            if na == c and a not in out:
                out.append(a)
                break
    # 再兜一层：子串包含（`stats` 含于 `query_cotton_stats`）
    if len(out) < limit:
        for a in available:
            if norm and (norm in normalized[a] or normalized[a] in norm) and a not in out:
                out.append(a)
            if len(out) >= limit:
                break
    return out[:limit]


def unknown_tool_message(name: str, available: list[str] | tuple[str, ...]) -> str:
    """**未知工具的统一提示文本** —— 原生路径与 MCP 路径共用这一份。

    ★ 这原来是**两处各写一份**的（`core/tools/__init__.py` 写"工具未找到"、
    `mcp_server.py` 写"未知工具"），正是本项目一直在防的"两份地图"。
    现在收敛到这里，两边都调它。

    ★ 首句仍然以 `工具未找到` 开头 —— 这是既有守卫
    （`check_tool_gate.py`）与 MCP 宿主都可能依赖的**稳定前缀**，不随意改。
    """
    avail = list(available)
    lines = [f"工具未找到: {name}"]
    sug = suggest_tool_names(name, avail)
    if sug:
        lines.append(f"你是不是想调用: {', '.join(sug)}")
    lines.append(f"可用工具: {', '.join(sorted(avail))}")
    lines.append("请用正确的工具名重新调用。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 防线二：参数多级降级解析
# ---------------------------------------------------------------------------

#: 解析层级名（写进日志与守卫断言，便于统计"模型最容易在哪一层失败"）
LEVEL_JSON = "json"
LEVEL_FENCED = "fenced"
LEVEL_EMBEDDED = "embedded"
LEVEL_LITERAL = "literal"
LEVEL_FAILED = "failed"

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


@dataclass(frozen=True)
class ParsedArguments:
    """参数解析结果。

    ★ `ok=False` 时 `args` 是**空 dict**，但调用方**不得**直接用它去执行工具 ——
    必须把 `error` 回灌给模型（这正是原来"静默降级成 `{}`"要修的地方）。
    """

    ok: bool
    args: dict[str, Any] = field(default_factory=dict)
    level: str = LEVEL_FAILED
    raw: str = ""
    error: str = ""

    def __bool__(self) -> bool:                              # noqa: D105
        return self.ok


def _try_json(text: str) -> dict[str, Any] | None:
    try:
        v = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def parse_tool_arguments(raw: Any) -> ParsedArguments:
    """把工具调用里的 `arguments` 解析成 dict，**四级降级**。

    | 层级 | 处理什么 |
    |---|---|
    | `json` | 标准 JSON（绝大多数情况） |
    | `fenced` | 模型套了 \\`\\`\\`json 围栏 |
    | `embedded` | JSON 前后夹了说明文字（抽第一个 `{...}`） |
    | `literal` | Python 字面量：单引号、`True/None`（`ast.literal_eval`） |
    | `failed` | 全失败 —— **返回错误，由调用方回灌给模型** |

    `dict` 入参直接通过（有些调用方已经解析好了）。
    None / 空串视为"无参数"→ 通过（`args={}`，level=`json`）。
    """
    if isinstance(raw, dict):
        return ParsedArguments(True, dict(raw), LEVEL_JSON, str(raw))
    if raw is None:
        return ParsedArguments(True, {}, LEVEL_JSON, "")
    text = str(raw)
    stripped = text.strip()
    if not stripped:
        return ParsedArguments(True, {}, LEVEL_JSON, text)

    # ① 标准 JSON
    got = _try_json(stripped)
    if got is not None:
        return ParsedArguments(True, got, LEVEL_JSON, text)

    # ② 剥围栏
    unfenced = _FENCE_RE.sub("", stripped).strip()
    if unfenced != stripped:
        got = _try_json(unfenced)
        if got is not None:
            return ParsedArguments(True, got, LEVEL_FENCED, text)

    # ③ 抽第一个 {...}（JSON 前后有说明文字；朴素但够用）
    body = unfenced or stripped
    start = body.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(body)):
            ch = body[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    got = _try_json(body[start:i + 1])
                    if got is not None:
                        return ParsedArguments(True, got, LEVEL_EMBEDDED, text)
                    break

    # ④ Python 字面量（单引号 / True / None）
    for candidate in (body, body[body.find("{"):] if "{" in body else body):
        cand = candidate.strip()
        if not cand:
            continue
        # 只对形如 {...} 的片段尝试，避免 literal_eval 把裸字符串当参数
        if not (cand.startswith("{") and cand.endswith("}")):
            continue
        try:
            v = ast.literal_eval(cand)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(v, dict):
            return ParsedArguments(True, {str(k): x for k, x in v.items()},
                                   LEVEL_LITERAL, text)

    # ⑤ 全失败 —— **不伪造空参数**
    preview = text[:160].replace("\n", " ")
    return ParsedArguments(
        False, {}, LEVEL_FAILED, text,
        error=("参数不是合法 JSON，无法解析。"
               f"你发的是：{preview!r}。"
               "请重新调用该工具，arguments 必须是**标准 JSON 对象**（键和字符串值都要双引号）。"),
    )


# ---------------------------------------------------------------------------
# 防线三：循环防护
# ---------------------------------------------------------------------------

#: 中文模型在"任务收尾"时高频输出这些句子 —— **说了等于没说**。
#: 与 AgentGPT 的 `real_tasks_filter`（过滤 "No tasks required"）同一个问题。
EMPTY_RESPONSE_PATTERNS: tuple[str, ...] = (
    "无需更多任务", "不需要更多任务", "没有更多任务", "无更多任务",
    "任务已完成", "任务完成", "已完成所有任务", "全部任务完成", "任务已全部完成",
    "无需继续", "不需要继续", "无需进一步", "无需其他操作", "无需操作",
)

#: 判定"空转"的最大长度 —— 超过这个长度就认为是**真回答**，不再判空转。
#: ★ 这条限制很重要：否则「任务已完成，产量 125 公斤/亩」会被误杀。
EMPTY_RESPONSE_MAX_CHARS = 40

_NOISE_RE = re.compile(r"[\s。.！!、,，~～…]")

__all__ = [
    "EMPTY_RESPONSE_MAX_CHARS", "EMPTY_RESPONSE_PATTERNS", "LEVEL_EMBEDDED",
    "LEVEL_FAILED", "LEVEL_FENCED", "LEVEL_JSON", "LEVEL_LITERAL",
    "LoopGuard", "NameResolution", "ParsedArguments", "TOOL_ALIASES",
    "is_empty_response", "normalize_name", "parse_tool_arguments",
    "resolve_tool_name", "suggest_tool_names", "unknown_tool_message",
]


def is_empty_response(text: Any) -> bool:
    """这段回答是不是"说了等于没说"的空转短句。

    ★★ **必须"全等"而不是"包含"** —— 这条是被实测抓出来的：
    最初写成 `pattern in core`（子串包含），于是
    「**任务已完成**，产量 125 公斤/亩」被判成空转，**把真实答案吞掉了**。

    宁可漏判（大不了不拦），也不能误杀（把有效回答丢掉）。
    误判方向必须是**保守的那一侧**。

    另外仍保留长度上限：超长文本直接判正常，省一次比对也避免意外。
    """
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t or len(t) > EMPTY_RESPONSE_MAX_CHARS:
        return False
    core = _NOISE_RE.sub("", t)
    if not core:
        return False
    return any(core == _NOISE_RE.sub("", p) for p in EMPTY_RESPONSE_PATTERNS)


@dataclass
class LoopGuard:
    """同一轮对话里，**相同 (工具, 参数) 重复调用**的防护。

    用法：
        guard = LoopGuard(max_repeat=1)
        blocked = guard.check(name, args)      # 非 None = 不该再执行
        if blocked: ... 把 blocked 当工具结果回灌 ...
        guard.remember(name, args, result)     # 执行后记录

    ★ 为什么"短路返回上次结果"而不是"直接拒绝"：
    模型需要**看到结果**才能继续推理；只给一句"别调了"它会不知所措。
    把上次结果连同一句提醒一起给它，它才有依据换路子。
    """

    max_repeat: int = 1
    #: (工具, 参数指纹) → 已调用次数
    _seen: dict[tuple[str, str], int] = field(default_factory=dict)
    #: (工具, 参数指纹) → 上次结果
    _last: dict[tuple[str, str], str] = field(default_factory=dict)
    #: ★ 轮次用尽、但模型**仍要求调用工具** —— 说明"思考—行动"的链条没走完。
    #:
    #: 这是 `docs/HANDOFF.md` 的移交触发信号。★ **它不是阈值，是观测到的事实**：
    #: 不需要比较任何数字，「最后一轮仍然要调工具」是一个**确定发生过的事件**。
    #: 观测不会误判，阈值会。
    _wants_more: bool = False
    #: 实际用掉的轮数（供日志与移交记录）
    _rounds_used: int = 0
    #: 已执行过的调用（**按执行顺序**）—— 移交时要把这些结果带进编排层，不重跑。
    _done: list[dict] = field(default_factory=list)

    def _key(self, name: str, args: Any) -> tuple[str, str]:
        try:
            fingerprint = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            fingerprint = str(args)
        return (str(name), fingerprint)

    def check(self, name: str, args: Any) -> str | None:
        """该不该**跳过执行**。返回提示文本（含上次结果），或 `None`（照常执行）。"""
        key = self._key(name, args)
        times = self._seen.get(key, 0)
        if times < self.max_repeat:
            return None
        last = self._last.get(key, "")
        preview = last[:300] + ("…" if len(last) > 300 else "")
        return (
            f"[重复调用] 你已经用**完全相同的参数**调用过 {name} {times} 次，结果不会变。\n"
            f"上次结果：{preview or '（无）'}\n"
            "请不要再用相同参数调用它 —— 换一个工具、改参数，或直接基于已有结果作答。"
        )

    def remember(self, name: str, args: Any, result: str) -> None:
        """记录一次执行（供后续 `check()` 判断）。"""
        key = self._key(name, args)
        self._seen[key] = self._seen.get(key, 0) + 1
        self._last[key] = str(result or "")
        self._done.append({"name": str(name), "args": args,
                           "result": str(result or "")})

    # ---- ★ 轮次耗尽观测（HANDOFF 第 1 步）----

    def mark_rounds_exhausted(self, rounds_used: int) -> None:
        """标记「轮次已经用尽，但模型在最后一轮**仍然要求**调用工具」。

        由 `rag_engine` 的工具循环在**自然结束**（没有 `break`）时调用。

        ★ 为什么这值得单独记一笔：以前这个事实**完全丢失** ——
          循环跑满就静默退出，之后照常走流式作答，
          模型拿着**不完整的中间结果**组织出一段"看起来完整"的答案。
          用户无从知道链条断了。这是**静默失败**。
        """
        self._wants_more = True
        self._rounds_used = max(0, int(rounds_used))

    @property
    def wants_more(self) -> bool:
        """轮次用尽时模型是否**仍想继续调用工具**。"""
        return self._wants_more

    @property
    def rounds_used(self) -> int:
        """实际用掉的轮数。"""
        return self._rounds_used

    def completed_calls(self) -> list[dict]:
        """已执行过的调用（按顺序）。

        ★ 移交时必须带上它 —— 那几轮**已经花掉了**，
          编排层从这些结果继续，而不是重头再查一遍。
        """
        return [dict(x) for x in self._done]


    def stats(self) -> dict[str, int]:
        """被拦下的次数统计（供观测）。"""
        return {f"{n}": c for (n, _), c in self._seen.items() if c > self.max_repeat}
