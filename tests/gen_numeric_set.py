# -*- coding: utf-8 -*-
"""生成数值集：从 CSV 抽样，记录期望值（保证 100% 可对拍）"""
import io, sys, os, json, random
import pandas as pd

ROOT = r"E:\cotton_agent"
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

random.seed(42)
DATA = "data/新疆_数据统计"
ydf = pd.read_csv(os.path.join(DATA, "新疆棉花产量年度统计.csv"), encoding="utf-8")
adf = pd.read_csv(os.path.join(DATA, "新疆棉花播种面积年度统计.csv"), encoding="utf-8")
mdf = pd.read_csv(os.path.join(DATA, "主要年份新疆棉花产量.csv"), encoding="utf-8")

Y_COL, A_COL = "棉花产量（吨）", "棉花播种面积（千公顷）"
LY_COL = "长绒棉产量（吨）"

regions = sorted([r for r in ydf["地区"].unique()
                  if isinstance(r, str) and "-" not in r])
regions = [r for r in regions if pd.notna(ydf[(ydf["地区"] == r)][Y_COL].iloc[0])]
print("可用地级地区数:", len(regions))

samples = []
nid = 0


def add(region, year, metric, value, unit, source):
    global nid
    nid += 1
    samples.append({
        "id": nid, "region": region, "year": int(year), "metric": metric,
        "expected_value": round(float(value), 1), "unit": unit, "source": source,
    })


# ① 产量单点 ×8
for r in random.sample(regions, min(8, len(regions))):
    row = ydf[(ydf["地区"] == r)].dropna(subset=[Y_COL])
    if row.empty:
        continue
    rec = row.sample(1, random_state=random.randint(0, 999)).iloc[0]
    add(r, rec["年份"], "yield", rec[Y_COL], "吨", "产量统计表")

# ② 面积单点 ×4
for r in random.sample(regions, min(4, len(regions))):
    row = adf[(adf["地区"] == r)].dropna(subset=[A_COL])
    if row.empty:
        continue
    rec = row.sample(1, random_state=random.randint(0, 999)).iloc[0]
    add(r, rec["年份"], "area", rec[A_COL], "千公顷", "面积统计表")

# ③ 亩产（计算） ×4 —— 公式：yield(吨)*1000 / (area(千公顷)*15000)
cnt = 0
for r in random.sample(regions, min(14, len(regions))):
    if cnt >= 4:
        break
    for year in random.sample(range(2015, 2023), 8):
        y = ydf[(ydf["地区"] == r) & (ydf["年份"] == year)]
        a = adf[(adf["地区"] == r) & (adf["年份"] == year)]
        if y.empty or a.empty:
            continue
        yv, av = y[Y_COL].iloc[0], a[A_COL].iloc[0]
        if pd.isna(yv) or pd.isna(av) or av == 0:
            continue
        mu = float(yv) * 1000 / (float(av) * 15000)
        add(r, year, "mu", mu, "公斤/亩", "产量÷面积计算")
        cnt += 1
        break

# ④ 长绒棉 ×2
cnt = 0
for r in random.sample(regions, min(20, len(regions))):
    if cnt >= 2:
        break
    row = ydf[(ydf["地区"] == r)].dropna(subset=[LY_COL])
    row = row[row[LY_COL] > 0]
    if row.empty:
        continue
    rec = row.iloc[-1]
    add(r, rec["年份"], "long_yield", rec[LY_COL], "吨", "长绒棉产量")
    cnt += 1

# ⑤ 全区主要年份 ×2
mv = mdf.dropna()
for rec in mv.sample(min(2, len(mv)), random_state=7).to_dict("records"):
    add("全区", rec[mv.columns[0]], "yield", rec[mv.columns[1]], "万吨", "主要年份表")

out = "tests/eval_data/numeric_set.jsonl"
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    for s in samples:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

print("已生成 %d 条 → %s" % (len(samples), out))
for s in samples:
    print("  #%2d %s | %s | %s = %s %s" % (s["id"], s["region"], s["year"],
                                           s["metric"], s["expected_value"], s["unit"]))
