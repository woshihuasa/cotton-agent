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


def web_search(args: dict) -> str:
    """调用 Tavily 联网搜索，返回拼接后的标题 + 摘要文本。"""
    query = args.get("query", "")
    if not query:
        return "Tavily 搜索失败"
    api_key = AppConfig.TAVILY_API_KEY
    if not api_key:
        return "错误：未配置 Tavily API Key"
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query, max_results=3, search_depth="advanced",
        )
        results = response.get("results", [])
        if not results:
            return "Tavily 搜索未返回有效结果。"
        lines: list[str] = []
        for r in results:
            title = r.get("title", "")
            content = r.get("content", "")
            if not title and not content:
                continue
            if title:
                lines.append(f"标题: {title}")
            if content:
                lines.append(f"摘要: {content}")
            lines.append("---")
        if not lines:
            return "Tavily 搜索未返回有效结果。"
        return "\n".join(lines)
    except Exception as e:
        # ⚠️ 这里**不能**用 print：本模块同时被 MCP stdio 服务使用，
        # 而 stdout 是 JSON-RPC 协议通道，任何非报文输出都会破坏协议帧。
        # 统一走 logging（默认落 stderr），桌面端与 MCP 端都安全。
        log.warning("Tavily 错误: %s", e)
        return f"Tavily 搜索失败，错误信息: {str(e)}。请告知用户搜索功能暂时不可用。"
