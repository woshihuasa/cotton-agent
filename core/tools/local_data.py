# -*- coding: utf-8 -*-
"""本地数据类工具：统计数据查询、价格指数、产量预测与风险分级。

三者都只读 `data/新疆_数据统计/` 下的 CSV（经 `core.cotton_stats` /
`core.cotton_price` / `core.yield_analysis`），**不写盘、不依赖 LLM**，
因此同样适合直接 MCP 暴露。

⚠️ `analyze_yield` 的 forecast / risk 分支会**生成图表并写盘**（沿用桌面端的
`![图表]` 兜底嵌入机制）。这条路径带有与 `plot.py` 相同的"本地路径耦合"，
MCP 场景下的改造方向见 ROADMAP 方向 D1。
"""
from __future__ import annotations

from core.cotton_price import (
    calc_pct_change,
    calc_percentile,
    calc_volatility,
    query_price,
)
from core.cotton_stats import (
    aggregate,
    query_major,
    query_value,
    rank_by_year,
)
from core.tools.json_utils import dumps
from core.yield_analysis import forecast_yield, risk_ranking


def query_cotton_stats(args: dict) -> str:
    """执行棉花统计数据查询，返回 JSON 字符串供 LLM 组织语言。"""

    metric = args.get("metric", "")
    stat = args.get("stat", "value")
    region = args.get("region", "")
    top = args.get("top", 5)

    # 非法 metric 安全校验（各查询函数已有校验，此处提前返回更友好的提示）
    valid_metrics = {"yield", "area", "mu", "long_yield", "long_area", "long_mu"}
    if metric not in valid_metrics:
        return dumps(
            {"error": f"不支持的指标: {metric!r}，可选 {sorted(valid_metrics)}"},
            ensure_ascii=False,
        )

    year = args.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            return dumps({"error": f"年份参数无效: {year}"}, ensure_ascii=False)

    if stat in ("total", "avg"):
        result = aggregate(region, metric, stat)
    elif stat == "rank":
        result = rank_by_year(year, metric, top)
    elif stat == "major":
        result = query_major(metric, year)
    else:  # value
        result = query_value(region, year, metric)

    return dumps(result, ensure_ascii=False)


def calc_price_volatility(args: dict) -> str:
    """执行棉花价格指数查询/计算，返回 JSON 字符串。"""

    metric = args.get("metric", "")
    grade = args.get("grade", "3128B")
    date = args.get("date")
    start = args.get("start")
    end = args.get("end")
    period = args.get("period", "monthly")

    if metric == "price":
        if not date:
            return dumps({"error": "price 查询需要 date 参数（YYYY-MM-DD 或 YYYY-MM）"},
                               ensure_ascii=False)
        result = query_price(grade, date)
    elif metric == "volatility":
        result = calc_volatility(grade, start, end, period)
    elif metric == "pct_change":
        result = calc_pct_change(grade, start, end)
    elif metric == "percentile":
        if not date:
            return dumps({"error": "percentile 查询需要 date 参数"},
                               ensure_ascii=False)
        result = calc_percentile(grade, date)
    else:
        result = {"error": f"不支持的 metric: {metric}，可选 price/volatility/pct_change/percentile"}

    return dumps(result, ensure_ascii=False)


def analyze_yield(args: dict) -> str:
    """执行产量预测/风险分级，返回 JSON 字符串。

    forecast 自动生成预测区间带图，risk 自动生成风险条形图，
    chart_path 随结果返回（复用现有 ![图表] 兜底嵌入机制）。
    """

    metric = args.get("metric", "")
    stat = args.get("stat", "")

    METRIC_LABELS = {
        "yield": "棉花产量", "area": "棉花播种面积",
        "long_yield": "长绒棉产量", "long_area": "长绒棉播种面积",
    }

    try:
        if stat == "forecast":
            region = args.get("region", "")
            if not region:
                return dumps(
                    {"error": "forecast 需要 region 参数（必须使用用户原话中的地区名）"},
                    ensure_ascii=False,
                )
            target_year = args.get("target_year")
            if target_year is None:
                return dumps(
                    {"error": "forecast 需要 target_year 参数（预测目标年份）"},
                    ensure_ascii=False,
                )
            confidence = args.get("confidence", 0.90)
            result = forecast_yield(region, metric, target_year, confidence)
            if "error" not in result:
                from core.trend_plot import generate_forecast_chart, next_chart_path
                out = next_chart_path("forecast")
                generate_forecast_chart(
                    result["region"], METRIC_LABELS.get(metric, metric),
                    result.get("unit", ""), result["history"],
                    result["target_year"], result["forecast"],
                    result["ci_lower"], result["ci_upper"], out,
                )
                result["chart_path"] = out.as_posix()
        elif stat == "risk":
            regions = args.get("regions")
            result = risk_ranking(metric, regions)
            if "error" not in result:
                from core.trend_plot import generate_risk_chart, next_chart_path
                out = next_chart_path("risk")
                generate_risk_chart(
                    result["ranking"], METRIC_LABELS.get(metric, metric),
                    result.get("unit", ""), out,
                )
                result["chart_path"] = out.as_posix()
        else:
            result = {"error": f"不支持的 stat: {stat}，可选 forecast/risk"}
    except Exception as e:
        result = {"error": f"分析失败: {e}"}

    return dumps(result, ensure_ascii=False)
