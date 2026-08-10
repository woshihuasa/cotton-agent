"""
产量预测区间与产区波动风险分级 — 分析层

数据源：复用 cotton_stats.series_by_year（2015-2022 分地区年度序列）。

功能：
  forecast_yield — 基于历史序列做线性趋势外推，返回点预测 + 置信区间
  risk_ranking  — 计算各地区变异系数（CV = std/mean）并分级

统计说明：
  - 8 年样本的线性趋势外推统计诚实但精度有限，结果仅供决策参考
  - 置信区间基于残差标准差（简化假设：残差独立同分布）
"""

from __future__ import annotations

import numpy as np

from core.cotton_stats import list_regions, series_by_year

# 支持分析的指标（mu 不支持：无面积外推时亩产预测不可靠）
ANALYZE_METRICS = {"yield", "area", "long_yield", "long_area"}

# 置信度 → z 值（正态近似）
_CONF_Z = {0.68: 1.0, 0.90: 1.645, 0.95: 1.96}

# 风险分级阈值（变异系数）
CV_LOW = 0.15
CV_HIGH = 0.35

RISK_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
}


def _z_value(confidence: float) -> float:
    """置信度 → z 值；不在表中时线性插值，非法值抛 ValueError。"""
    if confidence <= 0 or confidence >= 1:
        raise ValueError(f"置信度必须在 (0, 1) 区间内: {confidence}")
    keys = sorted(_CONF_Z)
    if confidence in _CONF_Z:
        return _CONF_Z[confidence]
    # 线性插值
    lo_k, hi_k = keys[0], keys[-1]
    if confidence < lo_k:
        return _CONF_Z[lo_k]
    if confidence > hi_k:
        return _CONF_Z[hi_k]
    for i in range(len(keys) - 1):
        a, b = keys[i], keys[i + 1]
        if a <= confidence <= b:
            t = (confidence - a) / (b - a)
            return _CONF_Z[a] + t * (_CONF_Z[b] - _CONF_Z[a])
    return _CONF_Z[hi_k]  # 不可达


def _classify_cv(cv: float) -> str:
    """变异系数 → 风险等级。"""
    if cv < CV_LOW:
        return "low"
    if cv < CV_HIGH:
        return "medium"
    return "high"


def forecast_yield(region: str, metric: str,
                   target_year: int, confidence: float = 0.90) -> dict:
    """基于历史序列线性趋势外推，预测某地区未来年份产量/面积。

    Args:
        region: 地区名（自动解析，支持兵团县级市别名）。
        metric: yield / area / long_yield / long_area。
        target_year: 预测目标年份（必须晚于最后一个样本年）。
        confidence: 置信度（0.68 / 0.90 / 0.95，其他值线性插值）。

    Returns:
        dict: 预测结果（预测值/区间/趋势/R²/样本信息）或 error。
    """
    if metric not in ANALYZE_METRICS:
        return {"error": f"不支持的指标: {metric}，可选 {sorted(ANALYZE_METRICS)}"}

    try:
        target = int(target_year)
        conf = float(confidence)
    except (TypeError, ValueError):
        return {"error": f"参数无效: target_year={target_year!r}, confidence={confidence!r}"}

    # 历史序列（取全量 2015-2022）
    seq = series_by_year(region, metric, 2015, 2022)
    if "error" in seq:
        return seq

    points = [(p["year"], p["value"]) for p in seq["points"] if p["value"] is not None]
    if len(points) < 4:
        return {"error": f"「{seq['region']}」有效样本仅 {len(points)} 年（<4），无法可靠预测",
                "region": seq["region"], "metric": metric}

    years = np.array([p[0] for p in points], dtype=float)
    values = np.array([p[1] for p in points], dtype=float)
    last_sample_year = int(years.max())

    if target <= last_sample_year:
        return {"error": f"目标年份 {target} 不是未来年份（历史数据到 {last_sample_year} 年）",
                "region": seq["region"], "metric": metric,
                "last_sample_year": last_sample_year}

    # 线性回归：value = a + b * (year - base)，base 取首年避免数值问题
    base = years.min()
    x = years - base
    slope, intercept = np.polyfit(x, values, 1)
    fitted = intercept + slope * x

    # 残差标准差（自由度 n-2）
    resid = values - fitted
    n = len(values)
    dof = max(n - 2, 1)
    resid_std = float(np.sqrt(np.sum(resid ** 2) / dof))

    # R²
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((values - values.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # 预测值 + 置信区间
    forecast_val = float(intercept + slope * (target - base))
    try:
        z = _z_value(conf)
    except ValueError as e:
        return {"error": str(e)}
    margin = z * resid_std

    # 趋势方向（斜率相对均值 > 1%/年 视为趋势明显）
    mean_val = float(values.mean())
    slope_pct = slope / mean_val if mean_val else 0.0
    if slope_pct > 0.01:
        trend = "增长"
    elif slope_pct < -0.01:
        trend = "下降"
    else:
        trend = "平稳"

    unit = seq.get("unit", "")
    return {
        "region": seq["region"],
        "metric": metric,
        "unit": unit,
        "target_year": target,
        "confidence": round(conf, 3),
        "forecast": round(forecast_val, 1),
        "ci_lower": round(forecast_val - margin, 1),
        "ci_upper": round(forecast_val + margin, 1),
        "trend": trend,
        "slope_per_year": round(slope, 1),
        "r2": round(r2, 3),
        "samples": n,
        "sample_range": f"{int(years.min())}-{last_sample_year}",
        "history": [{"year": int(y), "value": round(float(v), 1)}
                    for y, v in points],
        "note": "基于历史趋势的线性统计外推，仅供参考，实际产量受气候与政策影响可能偏离区间",
    }


def risk_ranking(metric: str = "yield", regions: list[str] | None = None) -> dict:
    """计算地区级产量波动风险分级（变异系数 CV = std/mean）。

    Args:
        metric: yield / area / long_yield / long_area。
        regions: 可选，仅分析指定地区（自动解析）；省略则分析全部地级行政区。

    Returns:
        dict: {"metric", "unit", "ranking": [{region, mean, std, cv, level, label}, ...],
               "note": 口径说明} 或 error。
    """
    if metric not in ANALYZE_METRICS:
        return {"error": f"不支持的指标: {metric}，可选 {sorted(ANALYZE_METRICS)}"}

    if regions:
        targets = []
        for r in regions:
            res = series_by_year(r, metric, 2015, 2022)
            if "error" in res:
                return {"error": f"地区「{r}」: {res['error']}",
                        "candidates": res.get("candidates")}
            targets.append(res["region"])
        region_names = targets
    else:
        # 全部地级行政区（排除总计行，series_by_year 不包含总计，直接取全部）
        region_names = [r for r in list_regions() if r != "总计"]

    unit = ""
    ranking = []
    skipped = []
    for name in region_names:
        seq = series_by_year(name, metric, 2015, 2022)
        if "error" in seq:
            skipped.append(name)
            continue
        unit = seq.get("unit", unit)
        vals = [p["value"] for p in seq["points"] if p["value"] is not None]
        if len(vals) < 4:
            skipped.append(name)
            continue
        arr = np.array(vals, dtype=float)
        mean = float(arr.mean())
        if mean == 0:
            skipped.append(name)
            continue
        std = float(arr.std(ddof=1))
        cv = std / mean
        level = _classify_cv(cv)
        ranking.append({
            "region": name,
            "mean": round(mean, 1),
            "std": round(std, 1),
            "cv": round(cv, 3),
            "level": level,
            "label": RISK_LABELS[level],
        })

    if not ranking:
        return {"error": "无有效地区数据可分级",
                "skipped": skipped}

    ranking.sort(key=lambda x: x["cv"], reverse=True)  # 风险从高到低

    result: dict = {
        "metric": metric,
        "unit": unit,
        "ranking": ranking,
        "cv_thresholds": {"low": f"<{CV_LOW}", "medium": f"{CV_LOW}-{CV_HIGH}",
                          "high": f">={CV_HIGH}"},
        "note": "风险分级基于 2015-2022 年变异系数（标准差/均值），低<0.15、中0.15-0.35、高≥0.35",
    }
    if skipped:
        result["skipped"] = skipped
    return result
