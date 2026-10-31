# -*- coding: utf-8 -*-
"""外部 API 类工具：实时天气（高德）与联网搜索（Tavily）。

两个工具都**无状态、不依赖本地数据**，因此是 MCP 暴露的最简子集 ——
可直接被任意 MCP 宿主调用（需宿主侧配好 `AMAP_API_KEY` / `TAVILY_API_KEY`）。
"""
from __future__ import annotations

import logging
from datetime import date

import requests

from config import AppConfig

log = logging.getLogger("CottonTools")


def get_weather(args: dict) -> str:
    """查询指定城市某天的天气（实况 / 未来 4 天预报）。"""
    city = args.get("city", "")
    if not city:
        return "天气查询失败: 未提供城市名。"
    date_str = args.get("date", "today")
    api_key = AppConfig.AMAP_API_KEY
    if not api_key:
        return "天气查询失败: 未配置高德地图 API Key。"

    # ── 计算目标日期相对于今天的偏移量 ──
    today = date.today()
    if date_str == "today":
        offset = 0
    elif date_str == "tomorrow":
        offset = 1
    else:
        try:
            target = date.fromisoformat(date_str)
            offset = (target - today).days
        except ValueError:
            return f"天气查询失败: 日期格式错误 [{date_str}]，请使用 YYYY-MM-DD 格式。"

    if offset < 0:
        return f"天气查询失败: 无法查询过去的日期 [{date_str}]。"

    try:
        if offset == 0:
            # 今天 → 实况天气 (extensions=base)
            resp = requests.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": api_key, "city": city, "extensions": "base"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "1":
                return f"天气查询失败: {data.get('info', '未知错误')}"
            lives = data.get("lives", [])
            if not lives:
                return f"天气查询失败: 未找到城市 [{city}] 的天气数据。"
            w = lives[0]
            return (
                f"城市: {w.get('city', city)}\n"
                f"天气: {w.get('weather', '未知')}\n"
                f"温度: {w.get('temperature', '未知')}°C\n"
                f"风向: {w.get('winddirection', '未知')} "
                f"风力: {w.get('windpower', '未知')}级\n"
                f"湿度: {w.get('humidity', '未知')}%\n"
                f"发布时间: {w.get('reporttime', '未知')}"
            )
        else:
            # 明天及以后 → 预报 (extensions=all), 最多 4 天
            if offset > 3:
                return (
                    f"天气查询失败: 仅支持查询今天起 4 天内的天气，"
                    f"{date_str} 超出预报范围（今天: {today}，最远可查: {today} 往后 3 天）。"
                )
            resp = requests.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": api_key, "city": city, "extensions": "all"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "1":
                return f"天气查询失败: {data.get('info', '未知错误')}"
            forecasts = data.get("forecasts", [])
            if not forecasts:
                return f"天气查询失败: 未找到城市 [{city}] 的预报数据。"
            casts = forecasts[0].get("casts", [])
            if offset >= len(casts):
                return f"天气查询失败: 高德 API 未返回 {date_str} 的预报数据。"
            t = casts[offset]
            return (
                f"城市: {forecasts[0].get('city', city)}\n"
                f"日期: {t.get('date', date_str)}\n"
                f"白天天气: {t.get('dayweather', '未知')}\n"
                f"夜间天气: {t.get('nightweather', '未知')}\n"
                f"白天温度: {t.get('daytemp', '未知')}°C\n"
                f"夜间温度: {t.get('nighttemp', '未知')}°C\n"
                f"白天风力: {t.get('daypower', '未知')}级\n"
                f"夜间风力: {t.get('nightpower', '未知')}级"
            )
    except requests.RequestException as e:
        return f"天气查询失败: 网络错误 - {e}"
    except (KeyError, IndexError, ValueError) as e:
        return f"天气查询失败: 数据解析错误 - {e}"
    except Exception as e:
        return f"天气查询失败: {e}"


#: 搜索后端。`auto` 时优先 Tavily（保持原有行为），没有 Key 再退 DeepSeek。
BACKENDS: tuple[str, ...] = ("auto", "tavily", "deepseek")


def pick_backend(want: str = "", *, tavily_key: str = "",
                 deepseek_key: str = "") -> tuple[str, str]:
    """决定这次用哪个后端。返回 `(后端名, 不可用原因)`；可用时原因是空串。

    ★ 纯函数，便于守卫直接测；也便于把"哪个 Key 有值"与"配置说什么"分开验证。

    `auto` 的口径：**优先 Tavily**。这样升级后默认行为与升级前逐字一致 ——
      否则同一句查询会突然换一家搜索引擎、结果排序也变了，而没人会想到是这个原因。
    """
    choice = str(want or "auto").strip().lower()
    if choice not in BACKENDS:
        return "", f"配置里的搜索后端 {choice!r} 不认识（可用：{list(BACKENDS)}）"
    if choice == "tavily":
        return ("tavily", "") if tavily_key else (
            "", "配置指定用 Tavily，但没有配置 TAVILY_API_KEY")
    if choice == "deepseek":
        return ("deepseek", "") if deepseek_key else (
            "", "配置指定用 DeepSeek Search，但没有配置 DEEPSEEK_API_KEY")
    # auto
    if tavily_key:
        return "tavily", ""
    if deepseek_key:
        return "deepseek", ""
    return "", "Tavily 与 DeepSeek 的 Key 都没有配置"


def web_search(args: dict) -> str:
    """联网搜索。**后端可切换**，输出格式与后端无关。

    · `auto`（默认）→ 有 Tavily Key 用 Tavily，否则用 DeepSeek Search；
    · `tavily` / `deepseek` → 强制指定，指定的那个缺 Key 就报错，**不静默换家**
      （静默换家会让"为什么结果变了"变成一桩无头案）。
    """
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "错误：搜索词为空"

    backend, why = pick_backend(
        AppConfig.WEB_SEARCH_BACKEND,
        tavily_key=AppConfig.TAVILY_API_KEY,
        deepseek_key=AppConfig.DEEPSEEK_API_KEY,
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
            headers={"Authorization": f"Bearer {AppConfig.DEEPSEEK_API_KEY}",
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
