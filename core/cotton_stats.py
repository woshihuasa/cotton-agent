"""
棉花统计数据访问层 — 查询 / 计算产量与面积数据

数据源：data/新疆_数据统计/ 下的 4 个统计 CSV：
  - 新疆棉花产量年度统计.csv        (2015-2022 分地区产量)
  - 新疆棉花播种面积年度统计.csv    (2015-2022 分地区面积)
  - 主要年份新疆棉花产量.csv        (1978-2022 全区产量)
  - 主要年份新疆棉花播种面积.csv    (1978-2022 全区面积)

指标约定：
  yield  产量（吨）
  area   播种面积（千公顷）
  mu     亩产（公斤/亩）＝ 产量(吨) ÷ 面积(千公顷) ÷ 10 × 1000 × 1000 / 15
          推导: 1 千公顷 = 15000 亩; 产量(吨) = 公斤数; 亩产(公斤/亩) = 产量(吨)*1000 / (面积(千公顷)*15000)
"""

from pathlib import Path

import pandas as pd

from config import AppConfig

# ── 路径常量 ──────────────────────────────────────────

_STATS_DIR = Path(AppConfig.DATA_DIR) / "新疆_数据统计"

YIELD_YEARLY_CSV = _STATS_DIR / "新疆棉花产量年度统计.csv"       # 分地区年度产量
AREA_YEARLY_CSV = _STATS_DIR / "新疆棉花播种面积年度统计.csv"    # 分地区年度面积
YIELD_MAJOR_CSV = _STATS_DIR / "主要年份新疆棉花产量.csv"        # 全区主要年份产量
AREA_MAJOR_CSV = _STATS_DIR / "主要年份新疆棉花播种面积.csv"     # 全区主要年份面积

# ── 列名常量 ──────────────────────────────────────────

COL_YEAR = "年份"
COL_REGION = "地区"
COL_YIELD = "棉花产量（吨）"
COL_YIELD_LONG = "长绒棉产量（吨）"
COL_AREA = "棉花播种面积（千公顷）"
COL_AREA_LONG = "长绒播种面积（千公顷）"
COL_YIELD_WT = "棉花产量（万吨）"
COL_YIELD_LONG_WT = "长绒棉产量（万吨）"
COL_AREA_MAJOR = "棉花播种面积（千公顷）"
COL_AREA_LONG_MAJOR = "长绒棉播种面积（千公顷）"

# 全区行政区名（年度表内，用于全区汇总，不含县级行）
PREFECTURE_REGIONS = {
    "总计", "乌鲁木齐市", "克拉玛依市", "吐鲁番市", "哈密市",
    "昌吉回族自治州", "伊犁哈萨克自治州", "塔城地区", "阿勒泰地区",
    "博尔塔拉蒙古自治州", "巴音郭楞蒙古自治州", "阿克苏地区",
    "克孜勒苏柯尔克孜自治州", "喀什地区", "和田地区", "生产建设兵团",
}


def _read_csv(path: Path) -> pd.DataFrame:
    """读取 CSV，空值转为空字符串。"""
    return pd.read_csv(path, dtype=str).fillna("")


def _yearly_df(csv_path: Path) -> pd.DataFrame:
    """读取年度表，规范化年份列为 int。"""
    df = _read_csv(csv_path)
    df[COL_YEAR] = pd.to_numeric(df[COL_YEAR], errors="coerce")
    df = df.dropna(subset=[COL_YEAR])
    df[COL_YEAR] = df[COL_YEAR].astype(int)
    return df


def _major_df(csv_path: Path) -> pd.DataFrame:
    """读取主要年份表（全区），规范化年份列为 int。"""
    df = _read_csv(csv_path)
    df[COL_YEAR] = pd.to_numeric(df[COL_YEAR], errors="coerce")
    df = df.dropna(subset=[COL_YEAR])
    df[COL_YEAR] = df[COL_YEAR].astype(int)
    return df


def _num(s: str) -> float | None:
    """字符串转 float；空/非法返回 None。"""
    s = str(s).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── 懒加载缓存 ────────────────────────────────────────

_cache: dict[str, pd.DataFrame] = {}


def _get_df(key: str) -> pd.DataFrame:
    if key not in _cache:
        if key == "yield_yearly":
            _cache[key] = _yearly_df(YIELD_YEARLY_CSV)
        elif key == "area_yearly":
            _cache[key] = _yearly_df(AREA_YEARLY_CSV)
        elif key == "yield_major":
            _cache[key] = _major_df(YIELD_MAJOR_CSV)
        elif key == "area_major":
            _cache[key] = _major_df(AREA_MAJOR_CSV)
    return _cache[key]


# ── 指标 → (表 key, 值列, 单位) 映射 ──────────────────
# yield/area = 棉花，long_yield/long_area = 长绒棉
METRIC_COLUMNS: dict[str, tuple[str, str, str]] = {
    "yield":      ("yield_yearly", COL_YIELD,       "吨"),
    "area":       ("area_yearly",  COL_AREA,        "千公顷"),
    "long_yield": ("yield_yearly", COL_YIELD_LONG,  "吨"),
    "long_area":  ("area_yearly",  COL_AREA_LONG,   "千公顷"),
}

# 主要年份表（全区，国家审定口径）
MAJOR_METRIC_COLUMNS: dict[str, tuple[str, str, str]] = {
    "yield":      ("yield_major", COL_YIELD_WT,       "万吨"),
    "area":       ("area_major",  COL_AREA_MAJOR,     "千公顷"),
    "long_yield": ("yield_major", COL_YIELD_LONG_WT,  "万吨"),
    "long_area":  ("area_major",  COL_AREA_LONG_MAJOR, "千公顷"),
}

METRIC_LABELS = {
    "yield": "棉花产量", "area": "棉花播种面积",
    "long_yield": "长绒棉产量", "long_area": "长绒棉播种面积",
    "mu": "棉花亩产", "long_mu": "长绒棉亩产",
}

# 支持 mu 计算的指标集合
MU_METRICS = {"mu", "long_mu"}


def series_by_year(region: str, metric: str,
                   start: int, end: int) -> dict:
    """返回某地区某指标在 [start, end] 年份区间内的年度序列。

    全区（region="全区" 或空）走主要年份表（单位：万吨 / 千公顷），
    分地区走年度统计表（单位：吨 / 千公顷）。亩产（mu/long_mu）逐年计算。

    Returns:
        dict: {"region": 规范化名称, "metric": metric, "unit": 单位,
               "points": [{"year": 2020, "value": 933733.0}, ...],  # value 可为 None
               "note": str|None}
    """
    region = (region or "").strip() or "全区"

    if metric not in METRIC_COLUMNS and metric not in MU_METRICS:
        return {"error": f"不支持的指标: {metric}，可选 yield/area/mu/long_yield/long_area/long_mu"}

    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError):
        return {"error": f"年份区间无效: start={start!r}, end={end!r}"}
    if start_i > end_i:
        start_i, end_i = end_i, start_i

    # ── 全区 → 主要年份表 ──
    if region in ("全区", "新疆", "新疆维吾尔自治区", "全疆"):
        if metric in MU_METRICS:
            return {"error": "主要年份表不支持亩产序列，请指定具体地区"}
        table_key, col, unit = MAJOR_METRIC_COLUMNS[metric]
        df = _get_df(table_key)
        sub = df[(df[COL_YEAR] >= start_i) & (df[COL_YEAR] <= end_i)] \
                 .sort_values(COL_YEAR)
        points = [{"year": int(r[COL_YEAR]), "value": _num(r[col])}
                  for _, r in sub.iterrows()]
        if not points:
            return {"error": f"全区 {start_i}-{end_i} 年无数据",
                    "available_years": list_years()}
        return {"region": "全区", "metric": metric, "unit": unit,
                "points": points}

    # ── 分地区 → 年度统计表 ──
    resolution = resolve_region(region)
    if not resolution.get("resolved"):
        candidates = resolution.get("candidates", [])
        if not candidates:
            return {"error": f"未找到与「{region}」相关的地区"}
        return {"error": f"「{region}」存在多个候选地区，请明确指定",
                "candidates": candidates}
    resolved = resolution["resolved"]

    if metric in MU_METRICS:
        yield_col = COL_YIELD if metric == "mu" else COL_YIELD_LONG
        area_col = COL_AREA if metric == "mu" else COL_AREA_LONG
        ydf = _get_df("yield_yearly")
        adf = _get_df("area_yearly")
        unit = "公斤/亩"
        points: list[dict] = []
        for year in range(start_i, end_i + 1):
            yrow = ydf[(ydf[COL_REGION] == resolved) & (ydf[COL_YEAR] == year)]
            arow = adf[(adf[COL_REGION] == resolved) & (adf[COL_YEAR] == year)]
            val = None
            if not yrow.empty and not arow.empty:
                y = _num(yrow.iloc[0][yield_col])
                a = _num(arow.iloc[0][area_col])
                if y is not None and a is not None and a > 0:
                    val = round(y * 1000 / (a * 15000), 1)
            points.append({"year": year, "value": val})
    else:
        table_key, col, unit = METRIC_COLUMNS[metric]
        df = _get_df(table_key)
        sub = df[(df[COL_REGION] == resolved) &
                 (df[COL_YEAR] >= start_i) & (df[COL_YEAR] <= end_i)] \
                 .sort_values(COL_YEAR)
        points = [{"year": int(r[COL_YEAR]), "value": _num(r[col])}
                  for _, r in sub.iterrows()]

    if not points:
        return {"error": f"「{resolved}」{start_i}-{end_i} 年无数据"}

    result: dict = {"region": resolved, "metric": metric, "unit": unit,
                    "points": points}
    if resolution.get("note"):
        result["note"] = resolution["note"]
    return result


# ── 地区解析 ──────────────────────────────────────────

# 兵团直辖县级市 → 生产建设兵团（数据并入兵团口径）
BINGTUAN_CITY_ALIASES: dict[str, str] = {
    "阿拉尔": "生产建设兵团", "图木舒克": "生产建设兵团", "五家渠": "生产建设兵团",
    "石河子": "生产建设兵团", "北屯": "生产建设兵团", "铁门关": "生产建设兵团",
    "双河": "生产建设兵团", "可克达拉": "生产建设兵团", "昆玉": "生产建设兵团",
    "阿拉尔市": "生产建设兵团", "图木舒克市": "生产建设兵团", "五家渠市": "生产建设兵团",
    "石河子市": "生产建设兵团", "北屯市": "生产建设兵团", "铁门关市": "生产建设兵团",
    "双河市": "生产建设兵团", "可克达拉市": "生产建设兵团", "昆玉市": "生产建设兵团",
}


def resolve_region(region: str) -> dict:
    """解析用户/LLM 传入的地区名，返回规范化地区或候选列表。

    规则链（按优先级）：
      1. 精确匹配
      2. 兵团城市别名表
      3. 去空格精确匹配
      4. 后缀匹配（"库车市" → "阿克苏地区-库车市"，唯一才采用）
      5. 包含匹配（"阿克苏" → "阿克苏地区"，唯一才采用）

    Returns:
        dict: {"resolved": str, "note": str|None}
           或 {"ambiguous": True, "candidates": [str, ...], "note": str}
    """
    region = (region or "").strip()
    if not region:
        return {"resolved": None, "note": "未提供地区名"}

    all_regions = list_regions()

    # 1. 精确匹配
    if region in all_regions:
        return {"resolved": region}

    # 2. 兵团城市别名表
    if region in BINGTUAN_CITY_ALIASES:
        target = BINGTUAN_CITY_ALIASES[region]
        return {"resolved": target,
                "note": f"{region}为兵团直辖县级市，数据并入「{target}」口径"}

    # 3. 去空格精确匹配
    compact = region.replace(" ", "").replace("　", "")
    for r in all_regions:
        if r.replace(" ", "").replace("　", "") == compact:
            return {"resolved": r}

    # 4. 后缀匹配：候选地区名以输入名结尾（如 "库车市" 命中 "阿克苏地区-库车市"）
    suffix_hits = [r for r in all_regions if r.endswith(region) and r != region]
    if len(suffix_hits) == 1:
        return {"resolved": suffix_hits[0],
                "note": f"「{region}」匹配到地区「{suffix_hits[0]}」"}

    # 5. 包含匹配：优先地级行政区（"阿克苏" → "阿克苏地区"），再放宽到全部
    pref_hits = [r for r in PREFECTURE_REGIONS if region in r and r != region]
    if len(pref_hits) == 1:
        return {"resolved": pref_hits[0],
                "note": f"「{region}」匹配到地区「{pref_hits[0]}」"}
    if len(pref_hits) > 1:
        return {"ambiguous": True, "candidates": pref_hits[:5],
                "note": f"「{region}」存在多个候选地区"}

    contains_hits = [r for r in all_regions if region in r and r != region]
    if len(contains_hits) == 1:
        return {"resolved": contains_hits[0],
                "note": f"「{region}」匹配到地区「{contains_hits[0]}」"}

    # 多候选：不猜测，交由候选直查
    if len(contains_hits) > 1:
        return {"ambiguous": True, "candidates": contains_hits[:5],
                "note": f"「{region}」存在多个候选地区"}
    if len(suffix_hits) > 1:
        return {"ambiguous": True, "candidates": suffix_hits[:5],
                "note": f"「{region}」存在多个候选地区"}

    return {"ambiguous": True, "candidates": [], "note": f"未找到与「{region}」相关的地区"}


# ── 公开查询接口 ──────────────────────────────────────

def list_regions() -> list[str]:
    """返回分地区年度表中所有地区名（含总计/兵团/统计分组）。"""
    return sorted(_get_df("yield_yearly")[COL_REGION].unique().tolist())


def list_years() -> list[int]:
    """返回分地区年度表覆盖的年份。"""
    return sorted(_get_df("yield_yearly")[COL_YEAR].unique().tolist())


def _query_value_raw(region: str, year: int, metric: str) -> dict:
    """单点查询（不做地区解析，供内部复用）。"""
    df_key, col, unit = METRIC_COLUMNS[metric]
    df = _get_df(df_key)
    row = df[(df[COL_REGION] == region) & (df[COL_YEAR] == year)]
    if row.empty:
        return {"error": f"未找到「{region}」{year} 年的数据（可查年份: {list_years()}）"}
    val = _num(row.iloc[0][col])
    return {"region": region, "year": year, "metric": metric, "value": val, "unit": unit,
            "has_data": val is not None}


def query_candidates(candidates: list, year: int, metric: str,
                     original: str | None = None, note: str | None = None) -> dict:
    """直接查询全部候选地区数据（歧义/未找到时替代单一报错）。

    Returns:
        dict: {"original", "note", "count", "regions_data": [...]}
    """
    regions_data = []
    for cand in candidates:
        if metric in MU_METRICS:
            regions_data.append(calc_mu(cand, year, metric))
        else:
            regions_data.append(_query_value_raw(cand, year, metric))
    return {"original": original, "note": note, "count": len(regions_data),
            "regions_data": regions_data}


def query_value(region: str, year: int, metric: str) -> dict:
    """查询某地区某年份的产量/面积/亩产。

    地区名自动解析：精确/别名/后缀/包含命中则直接查询；
    歧义或未找到时，直接返回全部候选地区的数据。

    Returns:
        dict: 含 value/unit 或 regions_data（候选直查）或 error。
    """
    if metric not in METRIC_COLUMNS and metric not in MU_METRICS:
        return {"error": f"不支持的指标: {metric}，可选 yield/area/mu/long_yield/long_area/long_mu"}

    resolution = resolve_region(region)

    if resolution.get("resolved"):
        resolved = resolution["resolved"]
        note = resolution.get("note")
        if metric in MU_METRICS:
            result = calc_mu(resolved, year, metric)
        else:
            result = _query_value_raw(resolved, year, metric)
        if note:
            result["note"] = note
        return result

    # 歧义或未找到 → 候选直查
    candidates = resolution.get("candidates", [])
    if not candidates:
        return {"error": f"未找到与「{region}」相关的地区",
                "available_years": list_years()}
    return query_candidates(candidates, year, metric,
                            original=region, note=resolution.get("note"))


def calc_mu(region: str, year: int, metric: str = "mu") -> dict:
    """计算某地区某年份棉花/长绒棉亩产（公斤/亩）。"""
    if metric not in MU_METRICS:
        return {"error": f"不支持的亩产指标: {metric}，可选 mu/long_mu"}

    resolution = resolve_region(region)
    if not resolution.get("resolved"):
        candidates = resolution.get("candidates", [])
        if not candidates:
            return {"error": f"未找到与「{region}」相关的地区"}
        return query_candidates(candidates, year, metric,
                                original=region, note=resolution.get("note"))
    region = resolution["resolved"]

    yield_col = COL_YIELD if metric == "mu" else COL_YIELD_LONG
    area_col = COL_AREA if metric == "mu" else COL_AREA_LONG
    yield_df = _get_df("yield_yearly")
    area_df = _get_df("area_yearly")

    yrow = yield_df[(yield_df[COL_REGION] == region) & (yield_df[COL_YEAR] == year)]
    arow = area_df[(area_df[COL_REGION] == region) & (area_df[COL_YEAR] == year)]
    if yrow.empty or arow.empty:
        return {"error": f"未找到「{region}」{year} 年的产量/面积数据"}

    y = _num(yrow.iloc[0][yield_col])
    a = _num(arow.iloc[0][area_col])
    if y is None or a is None or a <= 0:
        return {"region": region, "year": year, "metric": metric, "mu": None,
                "note": "该年产量或面积数据缺失，无法计算亩产"}

    mu = y * 1000 / (a * 15000)  # 公斤/亩
    return {"region": region, "year": year, "metric": metric, "mu": round(mu, 1),
            "yield": y, "area": a, "unit": "公斤/亩"}


def aggregate(region: str, metric: str, op: str) -> dict:
    """汇总：某地区多年合计/平均（产量/面积/长绒棉）。"""
    if metric not in METRIC_COLUMNS:
        return {"error": f"不支持的指标: {metric}，汇总仅支持 yield/area/long_yield/long_area"}
    df_key, col, unit = METRIC_COLUMNS[metric]
    df = _get_df(df_key)

    # 地区自动解析（全区/精确名直接使用，歧义返回候选提示）
    if region != "全区":
        resolution = resolve_region(region)
        if not resolution.get("resolved"):
            candidates = resolution.get("candidates", [])
            if not candidates:
                return {"error": f"未找到与「{region}」相关的地区"}
            return {"original": region, "note": resolution.get("note"),
                    "candidates": candidates,
                    "hint": "请对候选地区分别查询汇总"}
        region = resolution["resolved"]

    if region == "全区":
        # 全区汇总：取「总计」行（每年一行），按 op 累加/平均
        total_rows = df[df[COL_REGION] == "总计"]
        if total_rows.empty:
            return {"error": "未找到「总计」行数据"}
        vals = pd.to_numeric(total_rows[col], errors="coerce").dropna()
        if op == "total":
            v = vals.sum()
            label = "合计"
        elif op == "avg":
            v = vals.mean()
            label = "平均"
        else:
            return {"error": f"不支持的统计操作: {op}，可选 total/avg"}
        return {"region": "全区", "metric": metric, "op": label, "value": round(v, 1),
                "unit": unit, "years": len(vals),
                "note": "总计口径：含地方与生产建设兵团"}
    else:
        rows = df[df[COL_REGION] == region]
    if rows.empty:
        return {"error": f"未找到地区「{region}」的数据"}

    vals = pd.to_numeric(rows[col], errors="coerce").dropna()
    if vals.empty:
        return {"region": region, "metric": metric, "op": op, "value": None,
                "note": "该地区无有效数值"}

    if op == "total":
        v = vals.sum()
        label = "合计"
    elif op == "avg":
        v = vals.mean()
        label = "平均"
    else:
        return {"error": f"不支持的统计操作: {op}，可选 total/avg"}
    return {"region": region, "metric": metric, "op": label, "value": round(v, 1),
            "unit": unit, "years": len(vals)}


def rank_by_year(year: int, metric: str, top: int = 5) -> dict:
    """某年份各产区产量/面积排名（仅地级行政区，不含总计/县级/兵团）。"""
    if metric not in METRIC_COLUMNS:
        return {"error": f"不支持的指标: {metric}，排名仅支持 yield/area/long_yield/long_area"}
    df_key, col, unit = METRIC_COLUMNS[metric]
    df = _get_df(df_key)

    rows = df[(df[COL_YEAR] == year) & (df[COL_REGION].isin(PREFECTURE_REGIONS))]
    rows = rows[rows[COL_REGION] != "总计"]  # 保留兵团（2020 产量 213.4 万吨居首），仅排除总计
    vals = rows.copy()
    vals["_v"] = pd.to_numeric(vals[col], errors="coerce")
    vals = vals.dropna(subset=["_v"]).sort_values("_v", ascending=False)

    if vals.empty:
        return {"error": f"{year} 年无有效排名数据"}

    ranking = [
        {"region": r[COL_REGION], "value": round(r["_v"], 1)}
        for _, r in vals.head(top).iterrows()
    ]
    return {"year": year, "metric": metric, "unit": unit, "ranking": ranking}


def query_major(metric: str, year: int) -> dict:
    """查询全区主要年份数据（1978-2022，国家审定口径）。"""
    if metric not in MAJOR_METRIC_COLUMNS:
        return {"error": f"不支持的指标: {metric}，主要年份仅支持 yield/area/long_yield/long_area"}
    df_key, col, unit = MAJOR_METRIC_COLUMNS[metric]
    df = _get_df(df_key)
    row = df[df[COL_YEAR] == year]
    if row.empty:
        return {"error": f"主要年份表中无 {year} 年数据"}
    val = _num(row.iloc[0][col])
    return {"region": "全区", "year": year, "metric": metric,
            "value": val, "unit": unit, "caliber": "国家审定数"}
