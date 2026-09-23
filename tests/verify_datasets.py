# -*- coding: utf-8 -*-
"""校验四个评测数据集：格式、条数、字段完整性 + 数值集与 CSV 对拍"""
import io, sys, os, json
import pandas as pd

ROOT = r"E:\cotton_agent"
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

D = "tests/eval_data"
FILES = {
    "retrieval_set.jsonl": ("query", "expected_docs"),
    "tool_set.jsonl": ("prompt", "expected_tool"),
    "e2e_set.jsonl": ("question", "expected_tools"),
    "numeric_set.jsonl": ("region", "year", "metric", "expected_value"),
}
ok = True
data = {}
for fn, fields in FILES.items():
    p = os.path.join(D, fn)
    if not os.path.exists(p):
        print("[MISS] %s 不存在" % fn)
        ok = False
        continue
    rows = []
    for i, line in enumerate(open(p, encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            print("[ERR ] %s 第 %d 行 JSON 非法: %s" % (fn, i, e))
            ok = False
            continue
        miss = [f for f in fields if f not in obj]
        if miss:
            print("[ERR ] %s 第 %d 行缺字段 %s" % (fn, i, miss))
            ok = False
        rows.append(obj)
    data[fn] = rows
    print("[OK  ] %-22s %d 条，字段完整" % (fn, len(rows)))

# 数值集与 CSV 对拍
print("\n=== 数值集对拍（期望值 vs CSV 直读）===")
ydf = pd.read_csv("data/新疆_数据统计/新疆棉花产量年度统计.csv", encoding="utf-8")
adf = pd.read_csv("data/新疆_数据统计/新疆棉花播种面积年度统计.csv", encoding="utf-8")
mdf = pd.read_csv("data/新疆_数据统计/主要年份新疆棉花产量.csv", encoding="utf-8")
Y, A = "棉花产量（吨）", "棉花播种面积（千公顷）"
passed = failed = 0
for s in data.get("numeric_set.jsonl", []):
    r, y, m, exp = s["region"], s["year"], s["metric"], s["expected_value"]
    if r == "全区":
        # 全区走主要年份表（万吨），由下方专项校验处理
        continue
    try:
        if m == "yield":
            actual = float(ydf[(ydf["地区"] == r) & (ydf["年份"] == y)][Y].iloc[0])
        elif m == "long_yield":
            actual = float(ydf[(ydf["地区"] == r) & (ydf["年份"] == y)]["长绒棉产量（吨）"].iloc[0])
        elif m == "area":
            actual = float(adf[(adf["地区"] == r) & (adf["年份"] == y)][A].iloc[0])
        elif m == "mu":
            yv = float(ydf[(ydf["地区"] == r) & (ydf["年份"] == y)][Y].iloc[0])
            av = float(adf[(adf["地区"] == r) & (adf["年份"] == y)][A].iloc[0])
            actual = round(yv * 1000 / (av * 15000), 1)
        else:
            actual = None
        okrow = actual is not None and abs(actual - exp) < 0.05
        print("  %s #%d %s %s %s: 期望=%s 实际=%s" %
              ("PASS" if okrow else "FAIL", s["id"], r, y, m, exp, actual))
        passed += okrow
        failed += (not okrow)
    except Exception as e:
        print("  FAIL #%d %s: %s" % (s["id"], r, e))
        failed += 1

# 主要年份表（全区）
for s in data.get("numeric_set.jsonl", []):
    if s["region"] == "全区":
        row = mdf[mdf[mdf.columns[0]] == s["year"]]
        if not row.empty:
            actual = float(row[mdf.columns[1]].iloc[0])
            okrow = abs(actual - s["expected_value"]) < 0.05
            print("  %s 全区 #%d %s: 期望=%s 实际=%s" %
                  ("PASS" if okrow else "FAIL", s["id"], s["year"], s["expected_value"], actual))
            passed += okrow
            failed += (not okrow)

print("\n对拍结果: %d 通过, %d 失败" % (passed, failed))
print("总体:", "全部通过 ✓" if ok and failed == 0 else "存在问题 ✗")
