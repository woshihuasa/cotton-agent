# -*- coding: utf-8 -*-
"""数据范围一致性守卫 —— 防止"写死的范围"与真实数据脱节。

背景（2026-10-07 实测发现）
--------------------------
`core/cotton_price.py` 曾把数据范围**写死**为常量：

    DATA_RANGE_HINT = "数据范围: 2016-01-04 ~ 2022-12-30"

而 CSV 实际已到 **2026-07-31 / 2483 行**（注释里还写着"1580 个交易日"）。
该提示被用在 7 处错误信息里，会作为**工具结果回给 LLM** —— 于是用户问 2024 年
价格时，模型会照着一句过期提示回答"查不到"。**这是主动误导，不是文案瑕疵。**

修复方式是让范围**从数据现算**（`data_range_hint()`），与 `cotton_stats`
一贯的做法（`可查年份: {list_years()}`）对齐。本脚本守住这个性质。

用法:
    python tests/check_data_ranges.py
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import AppConfig                                    # noqa: E402
import core.cotton_price as cp                                  # noqa: E402
import core.cotton_stats as cs                                  # noqa: E402

DATA = Path(AppConfig.DATA_DIR) / "新疆_数据统计"
PASS = FAIL = 0


def chk(cond: bool, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK  ] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


def main() -> int:
    print("=" * 76)
    print("数据范围一致性守卫")
    print("=" * 76)

    # ── 一、价格层：提示必须等于 CSV 直读范围 ──
    print("\n[1] 价格指数（cotton_price）")
    raw = pd.read_csv(cp.PRICE_CSV, dtype=str)
    raw["日期"] = pd.to_datetime(raw["日期"])
    real_min, real_max = raw["日期"].min(), raw["日期"].max()
    hint = cp.data_range_hint()
    print(f"      CSV 真实范围 : {real_min:%Y-%m-%d} ~ {real_max:%Y-%m-%d}（{len(raw)} 行）")
    print(f"      data_range_hint() : {hint}")
    chk(f"{real_min:%Y-%m-%d}" in hint and f"{real_max:%Y-%m-%d}" in hint,
        "提示包含数据真实的起止日期")

    # 结构性守卫：不允许再出现写死的范围常量
    src = (ROOT / "core" / "cotton_price.py").read_text(encoding="utf-8")
    hard = re.findall(r'^\s*\w*RANGE\w*\s*=\s*["\']', src, flags=re.M)
    chk(not hard, "模块内不存在写死的范围常量", f"发现: {hard}")
    chk("DATA_RANGE_HINT" not in src, "旧的 DATA_RANGE_HINT 常量已彻底移除")

    # ── 二、价格层：越界查询的错误信息必须报真实范围 ──
    print("\n[2] 价格越界查询的错误提示")
    for probe_date in ("2000-01-03", "2099-12-31"):
        r = cp.query_price("3128B", probe_date)
        msg = str(r.get("error", ""))
        ok = msg and f"{real_max:%Y-%m-%d}" in msg
        chk(ok, f"查询 {probe_date} 的报错含真实上界 {real_max:%Y-%m-%d}", f"实际: {msg[:90]}")

    # ── 三、统计层：可查年份同样来自数据 ──
    print("\n[3] 统计数据（cotton_stats）")
    years = cs.list_years()
    yy = pd.read_csv(DATA / "新疆棉花产量年度统计.csv", dtype=str)
    col = "年份"
    y_min, y_max = int(yy[col].min()), int(yy[col].max())
    print(f"      CSV 年份范围: {y_min} ~ {y_max} ｜ list_years() 首尾: {years[0]} / {years[-1]}")
    chk(int(years[0]) == y_min and int(years[-1]) == y_max,
        "list_years() 与 CSV 年份范围一致")

    r = cs.query_value("阿克苏地区", 1900, "yield")
    msg = str(r.get("error", ""))
    chk("1900" in msg and str(y_max) in msg,
        f"越界年份报错含可查年份（应含 {y_max}）", f"实际: {msg[:110]}")

    # ── 四、schema 描述里的范围声明 ──
    print("\n[4] 对外 schema 的范围声明")
    from core.tools.schemas import TOOL_SCHEMAS                 # noqa: E402
    desc = {t["function"]["name"]: t["function"]["description"] for t in TOOL_SCHEMAS}
    price_desc = desc["calc_price_volatility"]
    chk("2026" in price_desc, "价格工具描述写的是 2026（与实际一致）",
        f"got: {price_desc[-60:]}")
    chk("2022-12" not in price_desc, "价格工具描述不含过期的 2022-12")

    print()
    print("=" * 76)
    print(f"结果: {PASS}/{PASS + FAIL} 通过")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
