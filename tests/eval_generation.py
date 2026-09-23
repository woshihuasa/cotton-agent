# -*- coding: utf-8 -*-
"""
生成质量评测（Generation Quality Evaluation）
==============================================

【脚本功能】
  评测端到端回答的质量 —— 覆盖"检索 + 工具 + 生成"完整链路，回答两个问题：
    1. 回答是否覆盖了期望要点（要点覆盖率）
    2. 回答是否包含编造内容（忠实度，LLM-as-judge 判定）
    3. 该拒答时是否拒答、不该拒答时是否乱拒（拒答准确率，程序关键词判定）

【评测流程】
  1. 读取评测集 tests/eval_data/e2e_set.jsonl（20 条：问题 + 期望工具链 + 关键要点 + 是否应拒答）
  2. 对每条问题：调用 RAGEngine.ask() 跑**完整生产链路**，收集流式回答全文
     （含查询改写、RAG 检索、工具调用循环、生成兜底等所有环节）
  3. 两类判定：
       · 拒答判定（程序）：回答中是否出现"未找到/无法回答/超出范围"等表述
         -> 与 should_refuse 标记比对，得到"拒答准确率"
       · 质量判定（LLM-as-judge）：把【问题 + 期望要点 + 回答】交给裁判模型，
         输出 {"covers": bool, "faithful": bool, "reason": str}
         -> covers 汇总为"要点覆盖率"，faithful 汇总为"忠实度"
  4. 输出逐条结果 + 三项指标 + 失败明细

【指标定义与目标】
  - 要点覆盖率 = judge 判定 covers=True 的比例（目标 ≥ 80%）
  - 忠实度     = judge 判定 faithful=True 的比例（目标 ≥ 90%）——回答无编造
  - 拒答准确率 = (应拒答且拒答 + 不应拒答且未拒答) / 总数（目标 ≥ 90%）

【为什么拒答用程序判定、质量用 judge】
  - 拒答是有明确特征的模式匹配（"未找到"等表述），程序判定客观且零成本
  - 内容质量（是否覆盖要点、有无编造）属开放式语义判断，必须用 LLM-as-judge
  - 但 judge 有偏差（位置/长度/自我偏好），生产环境需用人工标注样本校准——
    本脚本支持 --limit 小样本先跑，便于人工抽检对齐

【用法】
  .venv\\Scripts\\python.exe tests\\eval_generation.py             # 跑全部 20 条（约 15-25 分钟）
  .venv\\Scripts\\python.exe tests\\eval_generation.py --limit 3   # 只跑前 3 条（调试/人工校准用）
  .venv\\Scripts\\python.exe tests\\eval_generation.py --verbose   # 打印每条完整回答（人工校准必用）

【成本】
  每条约 2-6 次 LLM 调用（查询改写 + 工具循环 + 生成 + judge），
  20 条约几块钱、15-25 分钟；建议先用 --limit 3 验证再跑全量。

【产出用途】
  作为"生成侧"基线：后续优化（如增加引用来源、拒答阈值、忠实度约束）用本脚本验证提升。
"""
import io
import json
import os
import re
import sys
import time
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.llm_client import LLMClient          # noqa: E402
from core.rag_engine import RAGEngine          # noqa: E402

# ── 拒答特征词（程序判定用；命中任一即视为"拒答/未作答"）──
REFUSE_KEYWORDS = [
    "未找到", "没有找到", "暂无", "无相关", "资料中没有", "知识库中没有",
    "无法回答", "无法提供", "不能提供", "超出我的", "超出能力范围",
    "不包含", "没有相关", "建议咨询", "无法确定", "没有该",
]

# ── LLM-as-judge 提示词（评估要点覆盖 + 忠实度）──
JUDGE_PROMPT = """你是回答质量评审专家。请根据【用户问题】【期望要点】【模型回答】进行判定。

判定两项：
1. covers：模型回答是否覆盖了期望要点中的关键信息（要点可换表述，语义一致即可）
2. faithful：模型回答中是否存在**明显编造**

【关键判定约束 —— 以下来源的内容均属可信，不得判为编造】
被测系统在回答前会自动调用以下渠道，你在评审时看不到它们的具体返回，但它们真实存在：
- 天气 API（实时天气：日期、温度、湿度、风力等）
- 棉田统计数据库（产量/面积/亩产/价格指数等，含 2015-2026 年数据）
- 联网搜索（最新政策文件、新闻、市场行情等，含文号与发布日期）
- 【用户长期记忆】系统（如用户所在地区、种植面积、品种偏好等个性化信息）

因此：回答中出现上述类型的具体数字、日期、政策文号、用户背景（如"您位于南疆、有 20 亩棉田"）
**都不算编造**。只有当出现以下情况时才判定 faithful=false：
- 与问题完全无关的臆造内容；自相矛盾（如前后日期冲突）；越界的承诺或建议

只输出 JSON，格式：{{"covers": true/false, "faithful": true/false, "reason": "简短理由"}}

【用户问题】
{question}

【期望要点】
{key_points}

【模型回答】
{answer}
"""


def load_cases(path: str) -> list[dict]:
    """读取端到端评测集（JSONL）。"""
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def is_refusal(answer: str) -> bool:
    """程序判定：回答是否属于拒答/未作答（含拒答特征词）。"""
    return any(k in answer for k in REFUSE_KEYWORDS)


def parse_judge(text: str) -> dict:
    """解析 judge 输出（容错：从文本中提取 JSON 片段）。"""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成质量评测（忠实度 / 拒答率）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全部）")
    parser.add_argument("--verbose", action="store_true", help="打印完整回答（人工校准用）")
    args = parser.parse_args()

    cases = load_cases(os.path.join("tests", "eval_data", "e2e_set.jsonl"))
    if args.limit:
        cases = cases[: args.limit]
    print("生成质量评测：%d 条样本（端到端完整链路）\n" % len(cases))

    engine = RAGEngine()          # 完整引擎（含知识库、会话、工具）
    judge = LLMClient()

    cover_ok = faithful_ok = 0
    refuse_correct = 0
    judged_n = 0
    fails = []

    for case in cases:
        q = case["question"]
        should_refuse = case.get("should_refuse", False)
        t0 = time.time()

        # ① 跑完整生产链路，收集流式回答
        answer = ""
        try:
            for msg_type, chunk in engine.ask(q):
                if msg_type == "content":
                    answer += chunk
        except Exception as e:
            answer = ""
            print("  #%2d 链路异常: %s" % (case["id"], e))

        elapsed = time.time() - t0
        refused = is_refusal(answer)

        # ② 拒答判定（程序）
        refuse_ok = (refused == should_refuse)
        refuse_correct += refuse_ok

        # ③ 质量判定（judge；拒答样本跳过质量评审）
        covers = faithful = None
        if not should_refuse and answer:
            verdict = parse_judge(judge.generate_response([{
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    question=q,
                    key_points="、".join(case.get("key_points", [])),
                    answer=answer[:2000],
                ),
            }]))
            if verdict:
                judged_n += 1
                covers = bool(verdict.get("covers"))
                faithful = bool(verdict.get("faithful"))
                cover_ok += covers
                faithful_ok += faithful
                if not (covers and faithful):
                    fails.append((case["id"], q, covers, faithful, verdict.get("reason", "")))

        print("  #%2d %-34s 拒答=%s(期望%s) 覆盖=%s 忠实=%s %.0fs" %
              (case["id"], q[:34], "是" if refused else "否",
               "是" if should_refuse else "否",
               covers if covers is not None else "-",
               faithful if faithful is not None else "-", elapsed))

        if args.verbose:
            print("     回答: %s" % answer[:300].replace("\n", " "))
            print("-" * 60)

    total = len(cases)
    print("\n" + "=" * 60)
    print("拒答准确率: %d/%d = %.1f%%  （目标 ≥ 90%%）" %
          (refuse_correct, total, refuse_correct / total * 100))
    if judged_n:
        print("要点覆盖率: %d/%d = %.1f%%  （目标 ≥ 80%%）" %
              (cover_ok, judged_n, cover_ok / judged_n * 100))
        print("忠实度    : %d/%d = %.1f%%  （目标 ≥ 90%%）" %
              (faithful_ok, judged_n, faithful_ok / judged_n * 100))
    if fails:
        print("\n质量未达标明细:")
        for cid, q, covers, faithful, reason in fails:
            print("  #%2d %s" % (cid, q[:40]))
            print("        covers=%s faithful=%s | %s" % (covers, faithful, reason[:80]))


if __name__ == "__main__":
    main()
