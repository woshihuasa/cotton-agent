# -*- coding: utf-8 -*-
"""
评测总入口（Run All Evaluations）
==================================

【脚本功能】
  一键运行全部评测脚本，自动汇总结果并更新评测报告 eval_report.md。

【运行内容】
  1. verify_datasets.py  —— 评测集完整性校验（格式 + 数值集与 CSV 对拍）
  2. eval_numeric.py     —— 数值一致性（工具返回值 vs 期望值，目标 100%）
  3. eval_tools.py       —— 工具选择准确率（LLM 工具决策，支持多次运行评估稳定性）
  4. eval_retrieval.py   —— 检索质量 Hit@K / MRR（含 top-20 探针与未命中归因）
                            以 --rerank 运行，即与生产配置一致（向量召回 top-20 → 精排取 top-3）
  5. eval_generation.py  —— 生成质量（忠实度 / 拒答率 / 要点覆盖，LLM-as-judge）

【报告生成：自动区块 + 人工区块分离】
  eval_report.md 被划分为两部分：

  - **自动区块**：位于 `<!-- AUTO-BEGIN -->` 与 `<!-- AUTO-END -->` 之间，
    内容是「指标总览」与「各评测项明细」——**每次运行都会重建**。

  - **人工区块**：标记之外的其余章节（优化历程 / 缺陷案例 / 已知边界 / 复现命令），
    **运行时不触碰、由人工维护**。

  这样既保留"一键重跑刷新指标"的便利，又不会覆盖需要人工撰写的分析内容。
  若目标文件不存在，或存在但缺少标记，会先备份（eval_report.md.bak）再生成新骨架。

【用法】
  .venv\\Scripts\\python.exe tests\\run_all.py            # 完整评测
  .venv\\Scripts\\python.exe tests\\run_all.py --quick    # 快速模式

【产物】
  - eval_report.md              评测报告（自动区块重建，人工区块保留）
  - tests/eval_outputs/*.txt    各评测脚本的完整输出留档
"""
import io
import os
import re
import sys
import shutil
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

REPORT = "eval_report.md"
AUTO_BEGIN = "<!-- AUTO-BEGIN -->"
AUTO_END = "<!-- AUTO-END -->"

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
        # 纯 CSV 读取 + 静态扫描，**零 LLM 调用**，故放在最前面当"前置体检"
        "key": "data_ranges",
        "name": "数据范围一致性",
        "script": "tests/check_data_ranges.py",
        "args": [],
        "pattern": r"结果: (\d+/\d+ 通过)",
        "metric": "范围一致性",
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
        "name": "检索质量（Rerank 精排 · Hit@3 / MRR）",
        "script": "tests/eval_retrieval.py",
        # --rerank：与生产配置一致（向量召回 top-20 → bge-reranker-v2-m3 精排 → 取 top-3）
        "args": ["--rerank"],
        "pattern": r"Hit@3\s*: \d+/\d+ = ([\d.]+%)",
        "metric": "Hit@3",
    },
    {
        "key": "retrieval_recall",
        "name": "检索召回上限（Rerank 精排 · Hit@20 探针）",
        "script": "tests/eval_retrieval.py",
        "args": ["--rerank"],   # 与上一条同脚本同参数 → 复用同一份输出，不重复运行
        "pattern": r"Hit@20\s*: \d+/\d+ = ([\d.]+%)",
        "metric": "Hit@20",
        "only_pattern": True,   # 复用同一脚本输出，不重复运行
    },
    {
        "key": "generation",
        "name": "生成质量（忠实度 / 拒答率）",
        "script": "tests/eval_generation.py",
        "args": [],
        "pattern": r"忠实度\s*: \d+/\d+ = ([\d.]+%)",
        "metric": "忠实度",
    },
    # ── E4 记忆评测集（一个脚本跑一次，提取三项指标）──
    {
        "key": "memory",
        "name": "记忆质量（冲突消解 / 去重 / 检索）",
        "script": "tests/eval_memory.py",
        "args": [],
        "pattern": r"冲突消解准确率: \d+/\d+ = ([\d.]+%)",
        "metric": "冲突消解准确率（目标 ≥ 90%）",
    },
    {
        "key": "memory_dedup",
        "name": "记忆去重率",
        "script": "tests/eval_memory.py",
        "args": [],
        "pattern": r"去重率: \d+/\d+ = ([\d.]+%)",
        "metric": "去重率",
        "only_pattern": True,
    },
    {
        "key": "memory_retrieval",
        "name": "记忆检索 Hit@3",
        "script": "tests/eval_memory.py",
        "args": [],
        "pattern": r"记忆检索 Hit@3: \d+/\d+ = ([\d.]+%)",
        "metric": "Hit@3（目标 ≥ 80%）",
        "only_pattern": True,
    },
]

# ── 报告头部 ──────────────────────────────────────────────────
HEADER_TEMPLATE = """# 棉花智能问答助手 · 评测报告
"""

# ── 人工维护章节的骨架（仅在首次生成 / 缺少标记时使用）──────────
MANUAL_SKELETON = """\
## 三、优化历程（评测驱动迭代）

<!-- 人工维护：记录"改了什么 → 指标怎么变"，用于追溯每次优化的依据与效果 -->

| 日期 | 改动内容 | 效果 |
|---|---|---|
| — | — | — |

---

## 四、由评测发现的缺陷

<!-- 人工维护：每个案例记录「现象 / 根因 / 修复 / 验证」 -->

---

## 五、已知边界与后续计划

<!-- 人工维护：已知边界样本 + 待优化项（按评测项分组） -->

---

## 附：复现

```powershell
cd Your_path

# 全部评测
.venv\\Scripts\\python.exe tests\\run_all.py

# 单项运行
.venv\\Scripts\\python.exe tests\\verify_datasets.py          # 评测集校验
.venv\\Scripts\\python.exe tests\\check_data_ranges.py        # 数据范围一致性（零 LLM）
.venv\\Scripts\\python.exe tests\\eval_numeric.py             # 数值一致性
.venv\\Scripts\\python.exe tests\\eval_tools.py --repeat 3    # 工具选择
.venv\\Scripts\\python.exe tests\\eval_retrieval.py --rerank  # 检索质量（生产配置：Rerank 精排）
.venv\\Scripts\\python.exe tests\\eval_generation.py          # 生成质量

# 非评测类守卫（不进本报告的 AUTO 区块，各自独立运行）
.venv\\Scripts\\python.exe tests\\smoke_memory.py             # 记忆链路冒烟（零 LLM）
.venv\\Scripts\\python.exe tests\\smoke_mcp.py                # MCP 服务端冒烟
.venv\\Scripts\\python.exe tests\\check_imports.py            # 未使用导入检查
.venv\\Scripts\\python.exe tests\\check_l4_conflict.py        # L4 冲突判定回归
```
"""


def run_one(cfg: dict, quick: bool, cache: dict) -> dict:
    """运行单个评测脚本（同脚本复用缓存结果）。

    返回 `{ok, failed, output, metric}`：
      · `ok=False`      —— 脚本**不存在**（未实现）
      · `failed=True`   —— 脚本跑了但**退出码非 0**（本次检查未通过）

    为什么必须区分这两者：状态列原先只看"正则能不能解析出指标"，
    于是**一个确凿失败的检查（如 `5/9 通过`）照样显示 ✅** —— 守卫会形同虚设。
    现在退出码也纳入判定。
    """
    script = cfg["script"]
    if not os.path.exists(script):
        return {"ok": False, "failed": False, "output": "", "metric": "未实现（待补）"}

    # 同一脚本只跑一次（如 retrieval 与 retrieval_recall 共用输出）
    if script in cache:
        out, failed = cache[script]
    else:
        args = [PY, script] + cfg["args"]
        if not quick and cfg.get("repeat_args"):
            args += cfg["repeat_args"]
        print("  → 运行 %s ..." % script, flush=True)
        failed = False
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=1800, cwd=ROOT)
            out = (proc.stdout or "") + (proc.stderr or "")
            failed = proc.returncode != 0
        except subprocess.TimeoutExpired:
            out = "[超时] 脚本运行超过 30 分钟"
            failed = True
        cache[script] = (out, failed)
        # 留档完整输出
        with open(os.path.join(OUTDIR, os.path.splitext(os.path.basename(script))[0] + ".txt"),
                  "w", encoding="utf-8") as f:
            f.write(out)

    m = re.search(cfg["pattern"], out)
    metric = m.group(1) if m else "解析失败"
    return {"ok": True, "failed": failed, "output": out, "metric": metric}


def summarize(cfg: dict, res: dict, quick: bool) -> str:
    """生成报告中该评测项的一段摘要。"""
    lines = []
    if not res["ok"]:
        lines.append("- 状态：**未实现**（评测集已就绪，脚本待补）")
        return "\n".join(lines)
    if res.get("failed"):
        lines.append("- 状态：**本次运行未通过**（脚本退出码非 0，详见 `tests/eval_outputs/`）")
    if cfg.get("only_pattern"):
        lines.append("- 说明：与「检索质量」同一次运行，作为**召回上限**指标（诊断瓶颈在排序还是召回）")
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
    if cfg["key"] == "retrieval":
        hit1 = re.search(r"Hit@1\s*: (\d+/\d+ = [\d.]+%)", res["output"])
        mrr = re.search(r"MRR\s*: ([\d.]+)", res["output"])
        if hit1:
            lines.append("- Hit@1：**%s**" % hit1.group(1))
        if mrr:
            lines.append("- MRR：**%s**" % mrr.group(1))
        for m in re.finditer(r"^  【.+$", res["output"], re.M):
            lines.append("- " + m.group(0).strip())
    return "\n".join(lines)


def build_auto_block(results: dict, quick: bool, now: str) -> str:
    """构建自动区块正文（指标总览 + 各项明细）。"""
    lines = []
    lines.append("> **数据时间**：%s ｜ **运行模式**：%s"
                 % (now, "快速（工具集单次）" if quick else "完整（工具集 3 次）"))
    lines.append("> **检索口径**：`--rerank` 精排（向量召回 top-20 → 精排取 top-3），与生产配置一致")
    lines.append("> **说明**：本区块由 `tests/run_all.py` 自动重建（重跑即刷新）；"
                 "标记之外的章节为人工维护，不受影响。")
    lines.append("> **原始输出留档**：`tests/eval_outputs/`")
    lines.append("")

    lines.append("## 一、指标总览")
    lines.append("")
    lines.append("| 评测项 | 指标 | 结果 | 状态 |")
    lines.append("|---|---|---|---|")
    for cfg in EVALS:
        res = results[cfg["key"]]
        ok = (res["ok"] and not res.get("failed")
              and res["metric"] not in ("解析失败",))
        status = "✅" if ok else "⚠️ 需关注"
        lines.append("| %s | %s | %s | %s |" % (cfg["name"], cfg["metric"], res["metric"], status))
    lines.append("")

    lines.append("## 二、各评测项明细")
    for cfg in EVALS:
        if cfg.get("only_pattern"):
            continue
        lines.append("")
        lines.append("### %s" % cfg["name"])
        lines.append("")
        lines.append(summarize(cfg, results[cfg["key"]], quick))
    lines.append("")

    return "\n".join(lines)


def apply_to_report(auto_block: str) -> str:
    """把自动区块写入报告，保留人工维护的其余章节。返回处理说明。"""
    if not os.path.exists(REPORT):
        content = (HEADER_TEMPLATE + "\n" + AUTO_BEGIN + "\n\n" + auto_block
                   + "\n" + AUTO_END + "\n\n" + MANUAL_SKELETON)
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write(content)
        return "报告不存在 → 已生成新报告（含人工章节骨架）"

    with open(REPORT, encoding="utf-8") as f:
        text = f.read()

    # 标记必须"独占一行"才算数 —— 防止正文中内联引用标记字面量时被误匹配
    m_begin = re.search(r"^" + re.escape(AUTO_BEGIN) + r"\s*$", text, re.M)
    m_end = re.search(r"^" + re.escape(AUTO_END) + r"\s*$", text, re.M)
    if m_begin and m_end and m_end.start() > m_begin.end():
        head = text[:m_begin.start()]
        tail = text[m_end.end():]
        # m_end 的 `\s*$` 会把 AUTO-END 之后的空行一并吃掉 → 这里补回一个空行，
        # 保证自动区块与后续人工章节之间始终有空行（避免逐次重跑吃掉格式）
        tail = ("\n\n" + tail.lstrip("\n")) if tail.strip() else "\n"
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write(head + AUTO_BEGIN + "\n\n" + auto_block + "\n" + AUTO_END + tail)
        return "已重建自动区块（人工章节保持不变）"

    shutil.copy2(REPORT, REPORT + ".bak")
    content = (HEADER_TEMPLATE + "\n" + AUTO_BEGIN + "\n\n" + auto_block
               + "\n" + AUTO_END + "\n\n" + MANUAL_SKELETON)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(content)
    return ("原报告缺少 AUTO 标记 → 已备份为 %s.bak 并生成新结构，"
            "请把原有人工内容合并回来" % REPORT)


def main() -> None:
    parser = argparse.ArgumentParser(description="一键运行全部评测并更新报告")
    parser.add_argument("--quick", action="store_true", help="快速模式（工具集只跑 1 次）")
    args = parser.parse_args()

    print("=" * 60)
    print("棉花智能问答助手 · 全量评测")
    print("=" * 60)

    results, cache = {}, {}
    for cfg in EVALS:
        print("\n[%s]" % cfg["name"])
        results[cfg["key"]] = run_one(cfg, args.quick, cache)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    auto_block = build_auto_block(results, args.quick, now)
    note = apply_to_report(auto_block)

    print("\n" + "=" * 60)
    print("报告已更新: %s（%s）" % (REPORT, note))
    for cfg in EVALS:
        print("  %-24s %s" % (cfg["name"], results[cfg["key"]]["metric"]))


if __name__ == "__main__":
    main()
