# -*- coding: utf-8 -*-
"""外部 API 类工具：实时天气（高德）与联网搜索（Tavily）。

两个工具都**无状态、不依赖本地数据**，因此是 MCP 暴露的最简子集 ——
可直接被任意 MCP 宿主调用（需宿主侧配好 `AMAP_API_KEY` / `TAVILY_API_KEY`）。
"""
from __future__ import annotations

import logging

import requests

from config import AppConfig

log = logging.getLogger("CottonTools")


def get_weather(args: dict) -> str:
    """查询指定城市某天的天气。

    ★ 2026-11-01 改：取数搬到 `core/weather.py`，**中央气象台为主、高德兜底**。
      nmc 多给降水量、湿度、实况与 7 天预报；认不出的地名（如"阿克苏红旗坡农场"）
      再退回高德。数据源口径见 `WEATHER_SOURCE`。
    """
    city = str(args.get("city", "") or "").strip()
    if not city:
        return "天气查询失败: 未提供城市名。"
    date_str = str(args.get("date", "today") or "today")
    from core import weather as _weather
    text, why = _weather.fetch(city, date_str)
    if text:
        return text
    return f"天气查询失败: {why}"


#: 搜索后端。`auto` 时优先 Tavily（保持原有行为），没有 Key 再退 DeepSeek。
BACKENDS: tuple[str, ...] = ("auto", "tavily", "deepseek")


def pick_backend(want: str = "", *, tavily_key: str = "", deepseek_key: str = "",
                 tavily_on: bool = True,
                 deepseek_on: bool = True) -> tuple[str, str]:
    """决定这次用哪个后端。返回 `(后端名, 不可用原因)`；可用时原因是空串。

    ★ 纯函数，便于守卫直接测；也便于把"开关"与"Key"分开验证。

    【口径】`want` 现在表示**优先级**，不再是"强制"：
      · 能用的后端 = 开关打开 **且** 有 Key；
      · 优先用 `want`；它不可用就退到另一个（并记一条 info）；
      · 一个都没有 → 返回原因（界面上要能照着这句话去改）。

    ★ 为什么不再"强制"：开关本身就是显式的意图表达。既然用户能把 Tavily 关掉，
      那么"优先 Tavily"在他关掉 Tavily 之后就应当自然退到 DeepSeek ——
      而不是报一句"指定了 Tavily 却没有 Key"，让他去猜该改哪里。
    """
    want = str(want or "").strip().lower()
    if want not in ("", "auto", "tavily", "deepseek"):
        # .env 可能被手改坏。**不静默认账**：记一条警告，再按"优先 Tavily"走，
        # 免得一个拼写错误让联网搜索整个不可用。
        log.warning("WEB_SEARCH_BACKEND=%r 不认识（可用：tavily / deepseek），"
                    "按「优先 Tavily」处理", want)
    order = "deepseek" if want == "deepseek" else "tavily"

    avail: list[str] = []
    if tavily_on and tavily_key:
        avail.append("tavily")
    if deepseek_on and deepseek_key:
        avail.append("deepseek")

    if not avail:
        if not tavily_on and not deepseek_on:
            return "", ("两个搜索服务都关着 —— 在「设置 → 其他 API → 联网搜索」里"
                        "打开一个")
        return "", ("开着的搜索服务都没有密钥：Tavily 需要 TAVILY_API_KEY，"
                    "DeepSeek 搜索需要 DEEPSEEK_SEARCH_API_KEY"
                    "（留空则沿用 DEEPSEEK_API_KEY）")
    if order in avail:
        return order, ""
    other = avail[0]
    log.info("搜索后端：优先的 %s 不可用（关闭或没密钥），改用 %s", order, other)
    return other, ""


def web_search(args: dict) -> str:
    """联网搜索。**后端可切换**，输出格式与后端无关。

    · `auto`（默认）→ 有 Tavily Key 用 Tavily，否则用 DeepSeek Search；
    · `tavily` / `deepseek` → 强制指定，指定的那个缺 Key 就报错，**不静默换家**
      （静默换家会让"为什么结果变了"变成一桩无头案）。
    """
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "错误：搜索词为空"

    deepseek_key = (AppConfig.DEEPSEEK_SEARCH_API_KEY
                    or AppConfig.DEEPSEEK_API_KEY)
    backend, why = pick_backend(
        AppConfig.WEB_SEARCH_BACKEND,
        tavily_key=AppConfig.TAVILY_API_KEY,
        deepseek_key=deepseek_key,
        tavily_on=bool(getattr(AppConfig, "WEB_SEARCH_TAVILY_ON", True)),
        deepseek_on=bool(getattr(AppConfig, "WEB_SEARCH_DEEPSEEK_ON", True)),
    )
    if not backend:
        return f"错误：{why}"

    if backend == "deepseek":
        return _search_deepseek(query)
    return _search_tavily(query)


def _format_results(items: list[tuple[str, str]]) -> str:
    """统一的输出格式：`标题: …` / `摘要: …` / `---`。

    ★ 两个后端**必须产出同一种形状** —— 模型看到的输入不该因为它碰巧用了哪家
      而不同；否则提示词里的"标题/摘要"约定会时灵时不灵。
    """
    lines: list[str] = []
    for title, summary in items:
        title = str(title or "").strip()
        summary = str(summary or "").strip()
        if not title and not summary:
            continue
        if title:
            lines.append(f"标题: {title}")
        if summary:
            lines.append(f"摘要: {summary}")
        lines.append("---")
    return "\n".join(lines)


def _search_tavily(query: str) -> str:
    """Tavily 后端（原有实现，行为不变）。"""
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=AppConfig.TAVILY_API_KEY)
        response = client.search(
            query=query, max_results=3, search_depth="advanced",
        )
        results = response.get("results", [])
        if not results:
            return "搜索未返回有效结果。"
        text = _format_results([(r.get("title", ""), r.get("content", ""))
                                for r in results])
        return text or "搜索未返回有效结果。"
    except Exception as e:                                    # noqa: BLE001
        # ⚠️ 这里**不能**用 print：本模块同时被 MCP stdio 服务使用，
        # 而 stdout 是 JSON-RPC 协议通道，任何非报文输出都会破坏协议帧。
        # 统一走 logging（默认落 stderr），桌面端与 MCP 端都安全。
        log.warning("Tavily 错误: %s", e)
        return f"搜索失败（Tavily），错误信息: {e}。请告知用户搜索功能暂时不可用。"


def _search_deepseek(query: str) -> str:
    """DeepSeek Search 后端。

    ★ 用 `summary`（600–800 字）而不是 `snippet`（100–200 字）：这份结果最终
      是喂给模型的，短摘要信息量不够；`need_content=False` 省掉正文抓取的费用。
    ★ `count=3` 与 Tavily 那边的 `max_results=3` 对齐 —— 换后端不该顺带改变
      "一次搜索给模型几段材料"。
    """
    try:
        resp = requests.post(
            AppConfig.DEEPSEEK_SEARCH_URL,
            headers={"Authorization": "Bearer " + (
                AppConfig.DEEPSEEK_SEARCH_API_KEY or AppConfig.DEEPSEEK_API_KEY),
                     "Content-Type": "application/json"},
            json={"query": query, "count": 3, "need_content": False},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        log.warning("DeepSeek Search 网络错误: %s", e)
        return f"搜索失败（DeepSeek），网络错误: {e}。请告知用户搜索功能暂时不可用。"
    except ValueError as e:                                   # JSON 解析失败
        log.warning("DeepSeek Search 响应不是 JSON: %s", e)
        return f"搜索失败（DeepSeek），响应无法解析: {e}"

    if isinstance(payload, dict) and payload.get("error"):
        msg = (payload.get("error") or {}).get("message") or payload["error"]
        log.warning("DeepSeek Search 返回错误: %s", msg)
        return f"搜索失败（DeepSeek）: {msg}"

    results = (payload or {}).get("results") or []
    if not results:
        return "搜索未返回有效结果。"
    return _format_results([(r.get("title", ""), r.get("summary", "")
                             or r.get("snippet", "")) for r in results]) \
        or "搜索未返回有效结果。"
