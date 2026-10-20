# -*- coding: utf-8 -*-
"""打药适宜性判断的**参考脚本**（可选，非必需）。

【定位】按 SKILL.md 规范，`scripts/` 是**可选的参考材料**，不是可执行的工具入口 ——
        因为技能的加载机制是**提示注入**（内容进 prompt、模型照着走），
        而不是 Function Calling 注册（模型不能直接调它）。

        它的用途：① 把决策逻辑用代码写清楚，供人核对；② 将来升级为
        引擎执行的工作流（`docs/ORCHESTRATION.md`）时，作为节点实现的起点。
"""


def check_spray_conditions(weather: dict) -> str:
    """根据天气数据判断打药适宜性。

    与 SKILL.md「编排步骤」中的第 2–5 步一一对应。

    Args:
        weather: 天气数据（wind_level / rain / temp_avg）

    Returns:
        "适合" 或 "不适合：<原因>"
    """
    wind = weather.get("wind_level", 0)
    rain = weather.get("rain", False)
    temp = weather.get("temp_avg", 20)

    if wind >= 4:
        return f"不适合：风力{wind}级，药液会飘移"
    if rain:
        return "不适合：当天或次日有降雨，药液会被冲刷"
    if temp < 15 or temp > 32:
        return f"不适合：日均气温{temp}℃，药效差或有药害风险"
    return "适合"