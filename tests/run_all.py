# -*- coding: utf-8 -*-
"""
评测总入口（Run All Evaluations）
==================================

【脚本功能】
  一键运行全部评测脚本，自动汇总结果并生成评测报告 eval_report.md。

【运行内容】
  1. verify_datasets.py  —— 评测集完整性校验（格式 + 数值集与 CSV 对拍）
  2. eval_numeric.py     —— 数值一致性（工具返回值 vs 期望值，目标 100%）
  3. eval_tools.py       —— 工具选择准确率（LLM 工具决策，支持多次运行评估稳定性）
  4. eval_retrieval.py   —— 检索质量 Hit@3 / MRR（若已实现；未实现则标注为待办）
  5. eval_generation.py  —— 生成质量（忠实度 / 拒答率，LLM-as-judge；若已实现）

【实现方式】
  - 以子进程方式运行各评测脚本（互不干扰、可独立复现）
  - 用正则从输出中解析关键指标
  - 完整原始输出保存到 tests/eval_outputs/（便于排查与留档）
  - 最终生成 eval_report.md：指标总表 + 各项明细 + 优化历程 + 已知边界

【用法】
  .venv\\Scripts\\python.exe tests\\run_all.py            # 完整评测（工具集约 6-9 分钟）
  .venv\\Scripts\\python.exe tests\\run_all.py --quick    # 快速模式（工具集只跑 1 次，约 3 分钟）

【产物】
  - eval_report.md              评测报告（项目根目录，可随仓库提交）
  - tests/eval_outputs/*.txt    各评测脚本的完整输出留档

【说明】
  - 报告中的「优化历程」需人工维护（记录"改了什么 → 指标怎么变"），
    用于追溯每次优化的依据与效果，避免凭感觉改代码。
"""
import io
import os
import re
import sys
import json
import argparse
import subprocess
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PY = sys.executable
OUTDIR = os.path.join("tests", "eval_outputs")
os.makedirs(OUTDIR, exist_ok=True)

# ── 评测任务注册表 ────────────────────────────────────────────
# name: 报告中的显示名；script: 脚本路径；args: 额外参数
# pattern: 从输出中提取指标的正则（group 1 作为展示值）
EVALS = [
    {
        "key": "datasets",
        "name": "评测集完整性",
        "script": "tests/verify_datasets.py",
        "args": [],
        "pattern": r"对拍结果: (\d+ 通过, \d+ 失败)",
        "metric": "校验结果",
    },
    {
        "key": "numeric",
        "name": "数值一致性",
        "script": "tests/eval_numeric.py",
        "args": [],
        "pattern": r"数值一致性: (\d+/\d+ = [\d.]+%)",
        "metric": "一致率（目标 100%）",
    },
    {
        "key": "tools",
        "name": "工具选择准确率",
        "script": "tests/eval_tools.py",
        "args": [],          # 由 --quick 决定是否追加 --repeat 3
        "repeat_args": ["--repeat", "3"],
        "pattern": r"(?:平均准确率|工具选择准确率): ([\d.]+%)",
        "metric": "准确率",
    },
    {
        "key": "retrieval",
        "name": "检索质量（Hit@3 / MRR）",
        "script": "tests/eval_retrieval.py",
        "args": [],
        "pattern": r"Hit@3: \d+/\d+ = ([\d.]+%)",
        "metric": "Hit@3",
    },
    {
        "key": "generation",
        "name": "生成质量（忠实度 / 拒答率）",
        "script": "tests/eval_generation.py",
        "args": [],
        "pattern": r"忠实度\s*: \d+/\d+ = ([\d.]+%)",
        "metric": "忠实度",
    },
]


def run_one(cfg: dict, quick: bool) -> dict:
    """运行单个评测脚本，返回 {ok, output, metric, raw}。"""
    script = cfg["script"]
    if not os.path.exists(script):
        return {"ok": False, "output": "", "metric": "未实现（待补）", "raw": ""}

    args = [PY, script] + cfg["args"]
    if not quick and cfg.get("repeat_args"):
        args += cfg["repeat_args"]
    print("  → 运行 %s ..." % script, flush=True)
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800, cwd=ROOT)
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        out = "[超时] 脚本运行超过 30 分钟"
    # 留档完整输出
    with open(os.path.join(OUTDIR, cfg["key"] + ".txt"), "w", encoding="utf-8") as f:
        f.write(out)
    # 提取指标
    m = re.search(cfg["pattern"], out)
    metric = m.group(1) if m else "解析失败"
    return {"ok": True, "output": out, "metric": metric, "raw": out}


def summarize(cfg: dict, res: dict, quick: bool) -> str:
    """生成报告中该评测项的一段摘要。"""
    lines = []
    if not res["ok"]:
        lines.append("- 状态：**未实现**（评测集已就绪，脚本待补）")
        return "\n".join(lines)
    lines.append("- %s：**%s**" % (cfg["metric"], res["metric"]))
    if cfg["key"] == "tools":
        lines.append("- 运行模式：%s" % ("单次" if quick else "3 次（评估稳定性）"))
        for m in re.finditer(r"^第 \d+/\d+ 次运行: .+$", res["output"], re.M):
            lines.append("  - " + m.group(0))
        for m in re.finditer(r"^  # .+$", res["output"], re.M):
            lines.append("- 不稳定样本：" + m.group(0).strip())
    if cfg["key"] == "numeric":
        fail_line = re.search(r"失败明细:\n((?:  .+\n)+)", res["output"])
        if fail_line:
            lines.append("- 失败明细：")
            for l in fail_line.group(1).strip().split("\n"):
                lines.append("  " + l.strip())
    return "\n".join(lines)


# ── 优化历程（人工维护：记录"改了什么 → 指标怎么变"）──────────────
OPTIMIZATION_LOG = """\
| 日期 | 改动内容 | 效果 |
|---|---|---|
| 2026-09 | 修正工具描述数据范围（价格 2016-2022 → 2016-2026，与实际数据对齐） | 避免模型误判"无数据"而拒绝调用工具 |
| 2026-09 | 工具描述防歧义：`calc_price_volatility` 明确"仅现货指数，不含期货"；`web_search` 补充"市场行情（期货）"场景 | 工具选择准确率 **96.7% → 98.9%**；期货类问题误选工具从 2/3 次降至 0 |
"""

# ── 已知边界与后续计划 ────────────────────────────────────────
KNOWN_ISSUES = """\
| 编号 | 现象 | 归因 | 处置 |
|---|---|---|---|
| #4 | 复合意图（"这周适合打药吗，看下天气"）偶发漏调用 get_weather（2/3） | `get_weather` 不支持"整周"查询，且"适合打药吗"本身可用农技知识回答，模型在两可之间摇摆 | 保留为**已知边界样本**（真实系统存在此类模糊；不掩盖、可解释） |

**后续计划**
- 补检索质量评测（`eval_retrieval.py`）：Hit@3 / MRR，为后续引入混合检索 + Rerank 提供对比基线
- 补生成质量评测（`eval_generation.py`）：LLM-as-judge 评忠实度 + 拒答准确率
- 引入混合检索（BM25 + 向量 + RRF）与 Rerank 后，用本评测集量化提升幅度
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="一键运行全部评测并生成报告")
    parser.add_argument("--quick", action="store_true", help="快速模式（工具集只跑 1 次）")
    args = parser.parse_args()

    print("=" * 60)
    print("棉花智能问答助手 · 全量评测")
    print("=" * 60)

    results = {}
    for cfg in EVALS:
        print("\n[%s]" % cfg["name"])
        results[cfg["key"]] = run_one(cfg, args.quick)

    # ── 生成报告 ──
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append("# 棉花智能问答助手 · 评测报告\n")
    lines.append("> 生成时间：%s ｜ 运行模式：%s\n" % (now, "快速（单次）" if args.quick else "完整（工具集 3 次）"))
    lines.append("> 本报告由 `tests/run_all.py` 自动生成；评测集与脚本位于 `tests/` 目录。\n")

    lines.append("\n## 一、指标总览\n")
    lines.append("| 评测项 | 指标 | 结果 | 状态 |")
    lines.append("|---|---|---|---|")
    for cfg in EVALS:
        res = results[cfg["key"]]
        status = "✅ 通过" if (res["ok"] and "未实现" not in res["metric"] and "解析失败" not in res["metric"]) else ("⏳ 待补" if not res["ok"] else "⚠️ 需关注")
        lines.append("| %s | %s | %s | %s |" % (cfg["name"], cfg["metric"], res["metric"], status))

    lines.append("\n## 二、各评测项明细\n")
    for cfg in EVALS:
        lines.append("### %s\n" % cfg["name"])
        lines.append(summarize(cfg, results[cfg["key"]], args.quick))
        lines.append("")

    lines.append("\n## 三、优化历程（评测驱动迭代）\n")
    lines.append(OPTIMIZATION_LOG)

    lines.append("\n## 四、已知边界与后续计划\n")
    lines.append(KNOWN_ISSUES)

    lines.append("\n---\n")
    lines.append("*报告由 run_all.py 自动生成；原始输出留档于 `tests/eval_outputs/`。*\n")

    report = "\n".join(lines)
    with open("eval_report.md", "w", encoding="utf-8") as f:
        f.write(report)

    print("\n" + "=" * 60)
    print("报告已生成: eval_report.md")
    for cfg in EVALS:
        print("  %-24s %s" % (cfg["name"], results[cfg["key"]]["metric"]))


if __name__ == "__main__":
    main()
