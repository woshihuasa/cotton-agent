# -*- coding: utf-8 -*-
"""气象预警工具 —— 聊天里也能问"这边有没有预警"。

【为什么做成工具而不是写死在触发层】
  同一份数据有两个消费方：定时任务（到点检查用户所在地区）与聊天（用户主动问）。
  做成工具之后，两条路都走 `execute_tool_result` 这个唯一入口 —— 能力闸门、
  轨迹记录、MCP 暴露全都自动生效，不需要为聊天再开一条旁路。

【返回什么】
  - `summary`：几行中文，定时任务直接拿它当推送正文；
  - `items`：结构化条目（等级、信号、发布单位、时间、详情链接），聊天里可以细说。
"""
from __future__ import annotations

import json

from core import alarms


def get_weather_alerts(args: dict) -> str:
    """查询某地区**当前生效**的气象预警信号（中央气象台官方发布）。

    `region`：地区或城市名，按标题匹配（如「阿克苏」）；
    `area_code`：6 位行政区划代码，比名字精确（如「652901」）。
    两个都不给时返回全国范围内的前若干条（总量与条目）。
    """
    region = str(args.get("region") or args.get("city") or "").strip()
    code = str(args.get("area_code") or "").strip()

    alerts, error = alarms.fetch_alerts()
    if error:
        # ★ 拉不到 ≠ 没有预警。这里必须报错，让编排层判失败，
        #   绝不能回一句"当前无预警"把"不知道"说成"没事"。
        return json.dumps({"error": error, "summary": ""}, ensure_ascii=False)

    hits = alarms.match(alerts, area_hint=region, area_code=code)
    where = region or code or "全国"
    if hits:
        summary = (f"{where} 当前有 {len(hits)} 条生效的气象预警：\n"
                   + alarms.format_for_model(hits))
    else:
        summary = f"{where} 当前没有生效的气象预警信号。"

    return json.dumps({
        "region": where,
        "count": len(hits),
        "total_national": len(alerts),
        "items": [a.to_dict() for a in hits[:20]],
        "summary": summary,
    }, ensure_ascii=False)


__all__ = ["get_weather_alerts"]