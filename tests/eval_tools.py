# -*- coding: utf-8 -*-
"""
工具选择准确率评测（Tool Selection Evaluation）
================================================

【脚本功能】
  评测 LLM 面对用户问题时，是否能正确选择应调用的工具（Function Calling 决策质量）。
  这是 Agent 能力评测的核心指标之一 —— 工具选错，后续一切皆错。

【评测流程】
  1. 读取评测集 tests/eval_data/tool_set.jsonl（30 条：prompt → 期望工具）
  2. 对每条 prompt：把 6 个工具的 schema 交给 LLM，让它**只做工具选择决策**（不执行工具、不回答用户）
  3. 比对期望与实际：
       - expected_tool = "xxx"        → 模型应调用该工具
       - expected_tool = null         → 模型不应调用任何工具（知识库可答，考察"不过度调用"）
       - expected_tool = ["a", "b"]   → 歧义样本，命中任一即算对
  4. 输出逐条结果 + 总体准确率 + 误判分类统计
  5. --repeat N 支持重复运行 N 次：输出每次准确率、平均值，并列出**不稳定样本**
     （LLM 输出有随机性，单次评测不足以下结论；边界样本在不同轮次可能给出不同选择）

【指标定义】
  - 工具选择准确率 = 选择正确的条数 / 总条数
  - 附带统计"过度调用"（该不调却调了）、"漏调用"（该调却没调）、"选错工具"
  - 稳定性 = 每条样本在 N 次运行中通过的比例（<100% 即为边界/不稳定样本）

【用法】
  .venv\\Scripts\\python.exe tests\\eval_tools.py             # 跑全部 30 条（单次）
  .venv\\Scripts\\python.exe tests\\eval_tools.py --repeat 3  # 跑 3 次，看平均值与稳定性
  .venv\\Scripts\\python.exe tests\\eval_tools.py --limit 5   # 只跑前 5 条（调试用，省钱）
  .venv\\Scripts\\python.exe tests\\eval_tools.py --verbose   # 打印模型决策详情（含全部通过项）

【成本】
  每条每次 1 次 LLM 调用（30 条 × 3 次 ≈ 90 次，约几毛钱），无工具执行、无检索开销。

【注意】
  - 只断言"工具名"，不断言参数（参数会随措辞漂移，断言参数会造成假失败）
  - system prompt 保持精简、中立，避免诱导模型偏向某个工具
  - 不稳定样本不算"错"，而是提示该样本属边界情形（评测集可考虑放宽为多选）
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

from core.llm_client import LLMClient          # noqa: E402
from core.rag_engine import RAGEngine          # noqa: E402

# ── 评测用 system prompt（精简中立版，与生产 SYSTEM_PROMPT 的工具规则精神一致）──
EVAL_SYSTEM_PROMPT = (
    "你是新疆棉花种植助手。请根据用户的问题，判断应该调用哪个工具来获取所需信息；"
    "如果问题可以直接用农技知识回答（如栽培、施肥、灌溉、病虫害防治等），就不要调用工具。\n"
    "只做工具选择决策, 不要直接回答用户的问题。"
)


def load_cases(path: str) -> list[dict]:
    """读取工具评测集（JSONL）。"""
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def check(case: dict, actual: str | None) -> bool:
    """比对期望工具与实际选择（支持 null / 列表 / 字符串三种期望形式）。"""
    exp = case.get("expected_tool")
    if exp is None:                      # 期望不调用任何工具
        return actual is None
    if isinstance(exp, list):            # 歧义样本：命中任一即可
        return actual in exp
    return actual == exp


def classify_fail(case: dict, actual: str | None) -> str:
    """误判分类：过度调用 / 漏调用 / 选错工具。"""
    exp = case.get("expected_tool")
    if exp is None:
        return "过度调用"
    if actual is None:
        return "漏调用"
    return "选错工具"


def main() -> None:
    parser = argparse.ArgumentParser(description="工具选择准确率评测")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全部）")
    parser.add_argument("--repeat", type=int, default=1, help="重复运行次数（默认 1，用于评估稳定性）")
    parser.add_argument("--verbose", action="store_true", help="打印模型决策详情（含通过项）")
    args = parser.parse_args()

    cases = load_cases(os.path.join("tests", "eval_data", "tool_set.jsonl"))
    if args.limit:
        cases = cases[: args.limit]

    client = LLMClient()
    tools = RAGEngine._get_local_tool_schemas()
    total = len(cases)
    repeat = max(1, args.repeat)
    print("工具集评测：%d 条样本 × %d 次运行，%d 个候选工具\n" % (total, repeat, len(tools)))

    # 每条样本的通过次数（用于稳定性分析）
    case_pass = {c["id"]: 0 for c in cases}
    case_prompt = {c["id"]: c["prompt"] for c in cases}
    case_exp = {c["id"]: c.get("expected_tool") for c in cases}
    last_actual = {}      # 最近一次的实际选择（用于展示）
    run_rates = []
    fail_kinds = {}

    for run in range(1, repeat + 1):
        passed = 0
        for case in cases:
            msg = client.chat_with_tools(
                messages=[{"role": "system", "content": EVAL_SYSTEM_PROMPT},
                          {"role": "user", "content": case["prompt"]}],
                tools=tools,
            )
            # 提取模型选择的工具名（第一个 tool_call）
            actual = None
            if msg is not None and getattr(msg, "tool_calls", None):
                actual = msg.tool_calls[0].function.name

            ok = check(case, actual)
            passed += ok
            case_pass[case["id"]] += ok
            last_actual[case["id"]] = actual
            if not ok:
                kind = classify_fail(case, actual)
                fail_kinds[kind] = fail_kinds.get(kind, 0) + 1

        rate = passed / total * 100 if total else 0
        run_rates.append(rate)
        print("第 %d/%d 次运行: %d/%d = %.1f%%" % (run, repeat, passed, total, rate))

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if repeat > 1:
        print("平均准确率: %.1f%%  （最高 %.1f%% / 最低 %.1f%%）" %
              (sum(run_rates) / len(run_rates), max(run_rates), min(run_rates)))
    else:
        print("工具选择准确率: %.1f%%" % run_rates[0])
    if fail_kinds:
        print("误判分布（合计 %d 次）：%s" %
              (sum(fail_kinds.values()),
               " | ".join("%s %d" % (k, v) for k, v in sorted(fail_kinds.items()))))

    # 不稳定样本（通过次数 < 运行次数）
    unstable = [(cid, cnt) for cid, cnt in case_pass.items() if cnt < repeat]
    if unstable:
        print("\n不稳定/失败样本（通过次数 < 运行次数）：")
        for cid, cnt in unstable:
            print("  #%2d [%d/%d] %-30s 期望=%-42s 最近实际=%s" %
                  (cid, cnt, repeat, case_prompt[cid][:30],
                   str(case_exp[cid])[:42], last_actual[cid]))
    else:
        print("全部样本在 %d 次运行中均稳定通过 ✓" % repeat)


if __name__ == "__main__":
    main()
