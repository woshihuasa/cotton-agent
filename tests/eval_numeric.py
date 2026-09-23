# -*- coding: utf-8 -*-
"""
数值一致性评测（Numeric Accuracy Evaluation）
==============================================

【脚本功能】
  验证"工具返回的统计数据"与"CSV 原始数据"是否一致 —— 即 Agent 的数据准确性底线。
  该类问题（产量/面积/亩产）一旦算错，会直接误导用户，因此要求 100% 一致。

【评测流程】
  1. 读取评测集 tests/eval_data/numeric_set.jsonl（20 条：地区 + 年份 + 指标 + 期望值）
     · 期望值全部来自 CSV 直读或按公式计算，可逐条回溯
  2. 对每条样本，通过 RAGEngine._execute_tool 调用**真实工具链路**（query_cotton_stats）：
       - 分地区（地级市/县级）→ stat="value"，region + year
       - 全区（主要年份表）  → stat="major"，year
  3. 解析工具返回的 JSON，取 value 与期望值比对（容忍度 0.05，覆盖浮点与四舍五入差异）
  4. 输出逐条结果 + 一致性率（目标 100%）

【指标定义】
  - 数值一致性 = 一致条数 / 总条数（应恒为 100%；< 100% 说明数据链路有 bug）
  - 附带检查单位（unit）是否与期望一致

【覆盖的指标类型】
  yield（棉花产量，吨） / area（播种面积，千公顷） / mu（亩产，公斤/亩，公式计算）
  / long_yield（长绒棉产量，吨） / 全区主要年份（万吨）

【用法】
  .venv\\Scripts\\python.exe tests\\eval_numeric.py            # 跑全部 20 条
  .venv\\Scripts\\python.exe tests\\eval_numeric.py --verbose  # 打印工具原始返回

【成本】
  不调用 LLM，纯本地计算 + 工具执行，零 API 成本，秒级完成。
"""
import io
import json
import os
import sys
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.rag_engine import RAGEngine          # noqa: E402

TOLERANCE = 0.05    # 数值容忍度（覆盖浮点误差与一位小数四舍五入）


def load_cases(path: str) -> list[dict]:
    """读取数值评测集（JSONL）。"""
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def build_args(case: dict) -> dict:
    """根据样本构造 query_cotton_stats 的工具参数。"""
    region = case["region"]
    if region == "全区":
        # 全区数据来自"主要年份表"，对应工具的 stat="major"
        return {"metric": case["metric"], "stat": "major", "year": case["year"]}
    return {
        "metric": case["metric"],
        "stat": "value",
        "region": region,
        "year": case["year"],
    }


def call_tool(tool_args: dict) -> dict:
    """通过真实工具链路执行查询，返回解析后的 dict（失败返回 {"error": ...}）。"""
    raw = RAGEngine._execute_tool("query_cotton_stats", tool_args)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"error": "工具返回非法 JSON: %r" % str(raw)[:120]}


def main() -> None:
    parser = argparse.ArgumentParser(description="数值一致性评测")
    parser.add_argument("--verbose", action="store_true", help="打印工具原始返回")
    args = parser.parse_args()

    cases = load_cases(os.path.join("tests", "eval_data", "numeric_set.jsonl"))
    print("数值一致性评测：%d 条样本（容忍度 ±%s）\n" % (len(cases), TOLERANCE))

    passed = 0
    unit_mismatch = 0
    fails = []

    for case in cases:
        tool_args = build_args(case)
        result = call_tool(tool_args)
        # 数值提取：多数指标的值在 "value" 字段；
        # 但 mu（亩产）返回结构为 {..., "mu": 114.6, "yield": ..., "area": ...}，
        # 故回退到与 metric 同名的字段（mu / long_mu）。
        actual = result.get("value")
        if actual is None:
            actual = result.get(case["metric"])
        unit = result.get("unit")
        exp = float(case["expected_value"])

        if args.verbose:
            print("  #%2d %s %s %s → %s" % (case["id"], case["region"], case["year"],
                                             case["metric"], result))

        # 数值比对
        ok = isinstance(actual, (int, float)) and abs(float(actual) - exp) <= TOLERANCE

        # 单位比对（不一致只提示，不计入失败）
        if ok and unit and case.get("unit") and unit != case["unit"]:
            unit_mismatch += 1

        passed += ok
        if not ok:
            fails.append((case["id"], case["region"], case["year"], case["metric"],
                          exp, actual, result.get("error")))

        flag = "PASS" if ok else "FAIL"
        print("  %s #%2d %-22s %s %-11s 期望=%-10s 实际=%s" %
              (flag, case["id"], case["region"], case["year"], case["metric"], exp,
               actual if actual is not None else "N/A"))

    total = len(cases)
    rate = passed / total * 100 if total else 0
    print("\n" + "=" * 56)
    print("数值一致性: %d/%d = %.1f%%  （目标 100%%）" % (passed, total, rate))
    if unit_mismatch:
        print("提示: %d 条单位字段与期望不一致（不影响数值判定）" % unit_mismatch)
    if fails:
        print("\n失败明细:")
        for cid, region, year, metric, exp, act, err in fails:
            print("  #%2d %s %s %s: 期望=%s 实际=%s %s" %
                  (cid, region, year, metric, exp, act, ("| " + err) if err else ""))
    else:
        print("全部一致 ✓")


if __name__ == "__main__":
    main()
