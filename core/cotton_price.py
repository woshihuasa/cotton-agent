"""
棉花价格指数数据访问层 — 价格查询 / 波动率 / 涨跌幅 / 历史分位

数据源：data/新疆_数据统计/新疆棉花价格指数月度统计.csv
  - 中国棉花协会 CC Index 六等级（1129B/2129B/3128B/4128B/1228B/2227B）
  - 列：日期 + 6 等级 × (价格(元/吨) + 涨跌)
  - ⚠️ **起止日期与行数不在此写死**：数据会随更新增长，写死的数字必然过期。
    运行时用 `data_range_hint()` 现算。
    （曾把"2016-01-04 ~ 2022-12-30，1580 个交易日"写进注释，而实际已到
    2026-07-31 / 2483 行 —— 注释和提示一起过期，见下方 `data_range_hint` 说明。）
"""

from pathlib import Path

import pandas as pd

from config import AppConfig

# ── 路径与列名 ────────────────────────────────────────

PRICE_CSV = Path(AppConfig.DATA_DIR) / "新疆_数据统计" / "新疆棉花价格指数月度统计.csv"
COL_DATE = "日期"

# 等级 → CSV 价格列
GRADE_COLUMNS: dict[str, str] = {
    "1129B": "1129B价格（元/吨）",
    "2129B": "2129B价格（元/吨）",
    "3128B": "3128B价格（元/吨）",
    "4128B": "4128B价格（元/吨）",
    "1228B": "1228B价格（元/吨）",
    "2227B": "2227B价格（元/吨）",
}

_cache: pd.DataFrame | None = None


def _get_df() -> pd.DataFrame:
    """加载价格数据（懒加载 + 缓存）。"""
    global _cache
    if _cache is None:
        df = pd.read_csv(PRICE_CSV, dtype=str).fillna("")
        df[COL_DATE] = pd.to_datetime(df[COL_DATE])
        for col in GRADE_COLUMNS.values():
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values(COL_DATE).reset_index(drop=True)
        _cache = df
    return _cache


def data_range_hint() -> str:
    """返回数据**真实**的起止日期（从数据现算，不写死）。

    为什么必须现算（2026-10-07 修）：此处原为硬编码常量
    `"数据范围: 2016-01-04 ~ 2022-12-30"`，但 CSV 实际已到 **2026-07-31**
    （2483 行，而非注释里写的 1580 行）。该提示会随错误信息作为**工具结果回给
    LLM**，于是模型会据此告诉用户"2022 年之后的价格查不到" —— 数据明明存在。
    **这不是文案瑕疵，是主动误导模型**。

    对照：`cotton_stats` 一直用的是这个做法（错误信息里 `可查年份: {list_years()}`），
    本模块此前是唯一的例外。改成现算后，数据更新时提示自动跟随，不可能再过期。
    """
    df = _get_df()
    if df.empty:
        return "数据范围: 未知（数据为空）"
    return f"数据范围: {df[COL_DATE].min():%Y-%m-%d} ~ {df[COL_DATE].max():%Y-%m-%d}"


def _grade_col(grade: str) -> str:
    grade = (grade or "").strip().upper()
    if grade not in GRADE_COLUMNS:
        raise KeyError(grade)
    return GRADE_COLUMNS[grade]


def _parse_date(s: str, is_end: bool) -> pd.Timestamp:
    """解析日期；YYYY-MM 格式的结束日期取当月月末，起始日期取当月月初。"""
    ts = pd.Timestamp(s)
    if len(s.strip()) == 7 and s.strip()[4] == "-":  # YYYY-MM
        if is_end:
            return ts + pd.offsets.MonthEnd(0)
        return ts
    return ts


def _date_range(start: str | None, end: str | None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """解析日期范围；缺省时用全表范围。start/end 支持 YYYY-MM-DD 或 YYYY-MM。"""
    df = _get_df()
    lo = df[COL_DATE].min()
    hi = df[COL_DATE].max()
    if start:
        try:
            lo = _parse_date(start, is_end=False)
        except ValueError:
            raise ValueError(f"起始日期格式无效: {start}（支持 YYYY-MM-DD 或 YYYY-MM）")
    if end:
        try:
            hi = _parse_date(end, is_end=True)
        except ValueError:
            raise ValueError(f"结束日期格式无效: {end}（支持 YYYY-MM-DD 或 YYYY-MM）")
    if lo > hi:
        raise ValueError(f"起始日期晚于结束日期: {start} > {end}")
    return lo, hi


def _slice(grade: str, start: str | None, end: str | None) -> pd.DataFrame:
    """返回指定等级与日期区间的数据（日期升序）。"""
    df = _get_df()
    lo, hi = _date_range(start, end)
    return df[(df[COL_DATE] >= lo) & (df[COL_DATE] <= hi)]


def monthly_series(grade: str, start: str | None = None,
                   end: str | None = None) -> dict:
    """返回某等级在日期区间内的月末价格序列。

    按月聚合：每月取该月最后一个交易日的价格。

    Returns:
        dict: {"grade": grade, "unit": "元/吨",
               "points": [{"month": "2021-12", "value": 22107.0}, ...],
               "date_range": "2021-01 ~ 2022-12"}
    """
    try:
        col = _grade_col(grade)
    except KeyError:
        return {"error": f"不支持的等级: {grade}，可选 {sorted(GRADE_COLUMNS)}"}

    try:
        df = _slice(grade, start, end)
    except ValueError as e:
        return {"error": str(e)}

    if df.empty:
        return {"error": f"区间内无价格数据（{data_range_hint()}）"}

    points: list[dict] = []
    for _, grp in df.groupby(df[COL_DATE].dt.to_period("M")):
        last = grp.iloc[-1]
        val = last[col]
        points.append({
            "month": str(last[COL_DATE].date())[:7],
            "value": float(val) if pd.notna(val) else None,
        })
    return {
        "grade": grade, "unit": "元/吨", "points": points,
        "date_range": f"{points[0]['month']} ~ {points[-1]['month']}",
    }


# ── 公开查询接口 ──────────────────────────────────────

def query_price(grade: str, date: str) -> dict:
    """查询某等级在某日/某月的价格。

    date 支持 YYYY-MM-DD（取当日）或 YYYY-MM（取当月最后一个交易日）。
    """
    try:
        col = _grade_col(grade)
    except KeyError:
        return {"error": f"不支持的等级: {grade}，可选 {sorted(GRADE_COLUMNS)}"}

    df = _get_df()
    date = (date or "").strip()
    try:
        target = pd.Timestamp(date)
    except ValueError:
        return {"error": f"日期格式无效: {date}（支持 YYYY-MM-DD 或 YYYY-MM）"}

    # YYYY-MM 月份格式：统一取当月最后一个交易日（避免 pd.Timestamp 解析为月首导致取到月初价）
    if len(date) == 7 and date[4] == "-":
        month_rows = df[df[COL_DATE].dt.strftime("%Y-%m") == target.strftime("%Y-%m")]
        if month_rows.empty:
            return {"error": f"{date} 无价格数据（{data_range_hint()}）"}
        last = month_rows.iloc[-1]
        val = last[col]
        if pd.isna(val):
            return {"grade": grade, "month": target.strftime("%Y-%m"), "value": None,
                    "has_data": False}
        return {"grade": grade, "month": target.strftime("%Y-%m"),
                "date": str(last[COL_DATE].date()), "value": float(val),
                "unit": "元/吨", "has_data": True}

    # 精确日
    day_rows = df[df[COL_DATE] == target]
    if not day_rows.empty:
        val = day_rows.iloc[0][col]
        if pd.isna(val):
            return {"grade": grade, "date": str(target.date()), "value": None,
                    "has_data": False, "note": "该日价格缺失"}
        return {"grade": grade, "date": str(target.date()), "value": float(val),
                "unit": "元/吨", "has_data": True}

    return {"error": f"{date} 无价格数据（{data_range_hint()}）"}


def calc_volatility(grade: str, start: str | None = None, end: str | None = None,
                    period: str = "monthly") -> dict:
    """计算某等级在区间内的波动率。

    period:
      monthly — 区间内日收益率的标准差（即单日波动率）
      annual  — 年化波动率 = 日收益率 std × √n（n 为区间实际交易日数）
    """
    try:
        col = _grade_col(grade)
    except KeyError:
        return {"error": f"不支持的等级: {grade}，可选 {sorted(GRADE_COLUMNS)}"}

    try:
        df = _slice(grade, start, end)
    except ValueError as e:
        return {"error": str(e)}

    if df.empty:
        return {"error": f"区间内无价格数据（{data_range_hint()}）"}

    prices = df[col].dropna()
    if len(prices) < 2:
        return {"grade": grade, "volatility": None,
                "note": "有效价格数据不足 2 个交易日，无法计算波动率"}

    returns = prices.pct_change().dropna()
    if returns.empty:
        return {"grade": grade, "volatility": None, "note": "价格无变化，无法计算波动率"}

    daily_std = returns.std()
    n = len(returns)

    if period == "annual":
        vol = daily_std * (n ** 0.5)
        label = "年化波动率"
    else:
        vol = daily_std
        label = "日波动率（月内日收益标准差）"

    return {
        "grade": grade, "period": label, "volatility": round(vol * 100, 2),
        "unit": "%", "trading_days": n,
        "date_range": f"{df.iloc[0][COL_DATE].date()} ~ {df.iloc[-1][COL_DATE].date()}",
    }


def calc_pct_change(grade: str, start: str | None = None, end: str | None = None) -> dict:
    """计算某等级在区间内的涨跌幅（%）。"""
    try:
        col = _grade_col(grade)
    except KeyError:
        return {"error": f"不支持的等级: {grade}，可选 {sorted(GRADE_COLUMNS)}"}

    try:
        df = _slice(grade, start, end)
    except ValueError as e:
        return {"error": str(e)}

    if df.empty:
        return {"error": f"区间内无价格数据（{data_range_hint()}）"}

    prices = df[col].dropna()
    if len(prices) < 2:
        return {"grade": grade, "pct_change": None, "note": "有效价格不足，无法计算涨跌幅"}

    first = prices.iloc[0]
    last = prices.iloc[-1]
    if first == 0:
        return {"grade": grade, "pct_change": None, "note": "期初价格为 0，无法计算涨跌幅"}

    pct = (last / first - 1) * 100
    return {
        "grade": grade, "pct_change": round(pct, 2), "unit": "%",
        "start_date": str(df.iloc[0][COL_DATE].date()),
        "end_date": str(df.iloc[-1][COL_DATE].date()),
        "start_price": float(first), "end_price": float(last), "unit_price": "元/吨",
    }


def calc_percentile(grade: str, date: str) -> dict:
    """计算某等级某日在全历史区间的价格分位（0-100）。"""
    try:
        col = _grade_col(grade)
    except KeyError:
        return {"error": f"不支持的等级: {grade}，可选 {sorted(GRADE_COLUMNS)}"}

    df = _get_df()
    date = (date or "").strip()
    try:
        target = pd.Timestamp(date)
    except ValueError:
        return {"error": f"日期格式无效: {date}"}

    # YYYY-MM 月份格式：统一取当月最后一个交易日（避免 pd.Timestamp 解析为月首）
    if len(date) == 7 and date[4] == "-":
        month_rows = df[df[COL_DATE].dt.strftime("%Y-%m") == target.strftime("%Y-%m")]
        if month_rows.empty:
            return {"error": f"{date} 无价格数据（{data_range_hint()}）"}
        day_rows = month_rows.iloc[[-1]]
        target = day_rows.iloc[0][COL_DATE]
    else:
        day_rows = df[df[COL_DATE] == target]
        if day_rows.empty:
            return {"error": f"{date} 无价格数据（{data_range_hint()}）"}

    val = day_rows.iloc[0][col]
    if pd.isna(val):
        return {"grade": grade, "date": str(target.date()), "percentile": None,
                "note": "该日价格缺失"}

    hist = df[col].dropna()
    pct = (hist < val).mean() * 100
    return {
        "grade": grade, "date": str(target.date()), "price": float(val),
        "unit": "元/吨", "percentile": round(pct, 1), "unit": "%",
        "note": "分位为历史区间内低于该价格的交易日占比",
    }
