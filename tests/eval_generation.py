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
import shutil
import sys
import time
import argparse
from datetime import date
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import AppConfig                   # noqa: E402
from core.llm_client import LLMClient          # noqa: E402
from core.rag_engine import RAGEngine          # noqa: E402

# ── 拒答特征词（程序判定用；命中任一即视为"拒答/未作答"）──
#
# ⚠️ 这是一份**关键词表**，天然存在覆盖缺口 —— 2026-10-07 实测踩到过一次：
#    样本 #17「比特币今天的价格是多少」的回答是
#        「# 抱歉，这个问题我回答不了 … 不在我的服务范围内 …」
#    措辞完全得当，却**不含下表任何一词**，于是被判为"未拒答"（**测量缺陷**，
#    与 5.5 记录的"回答被截断"「judge 上下文缺失」同属"评测方法自身出错"一类）。
#    故补充了下方的口语化表达。
#
# ⚠️ 已知残余风险：`建议咨询` 偏宽泛 —— 正常回答里出现「建议咨询当地农技部门」
#    也会被判为拒答。当前 20 条集里未触发，但扩充评测集时需留意，
#    更稳妥的做法是改为"位置限定"（如仅当该词出现在回答前 1/3）或交给 judge 判定。
REFUSE_KEYWORDS = [
    "未找到", "没有找到", "暂无", "无相关", "资料中没有", "知识库中没有",
    "无法回答", "无法提供", "不能提供", "超出我的", "超出能力范围",
    "不包含", "没有相关", "建议咨询", "无法确定", "没有该",
    # ── 2026-10-07 补充：口语化拒答（#17 实测漏判）──
    "回答不了", "不在我的服务范围", "不在服务范围", "帮不上",
    "无法为您", "无法查证", "不具备", "不便回答", "无此",
]

# ── LLM-as-judge 提示词（评估要点覆盖 + 忠实度）──
JUDGE_PROMPT = """你是回答质量评审专家。请根据【用户问题】【期望要点】【模型回答】进行判定。

判定两项：
1. covers：模型回答是否覆盖了期望要点中的关键信息（要点可换表述，语义一致即可）
2. faithful：模型回答中是否存在**明显编造**

【本次评审的当前日期】
{today_date}

**判断"未来数据"时必须以上述日期为准，不要使用你自身知识中的日期。**
被测系统可能返回该日期之后不久的数据（行情/天气/统计口径等），只要与问题相关即属正常。

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


def _make_eval_sandbox() -> Path:
    """构建评测沙箱：把**固定 fixture** 与**会话存档快照**复制进独立目录。

    评测必须跑在隔离作用域上（ROADMAP S2），否则会写生产存档与记忆库（短板 #18）。

    为什么两样都要复制：
      · `chroma_user_memory/`  ← S0 冻结的 L4 fixture（620 + 44 条），保证记忆上下文一致
      · `session_state.json`   ← 当前生产存档快照，保证 L2 历史 / L3 摘要一致
    只复制其一都会让生成指标与旧基线（忠实度 94.4%）失去可比性——旧基线是在
    "带着真实 L2/L3/L4"的条件下测出来的。

    每次运行都重建沙箱，因此评测的**初始状态是确定的**（这正是 E4 记忆评测集的前提）。
    """
    root = Path(__file__).resolve().parent.parent
    fixture_db = root / "tests" / "eval_fixtures" / "memory" / "db"
    sandbox = root / "tests" / "eval_outputs" / "memory_sandbox"

    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)

    if fixture_db.exists():
        shutil.copytree(fixture_db, sandbox / "chroma_user_memory")
        print(f"[Sandbox] L4 fixture 已载入（{fixture_db}）")
    else:
        print(f"[Sandbox] ⚠️ 未找到 L4 fixture（{fixture_db}）——本次以空记忆运行")

    prod_state = Path(AppConfig.SESSION_FILE_PATH)
    if prod_state.exists():
        shutil.copy2(prod_state, sandbox / "session_state.json")
        print("[Sandbox] 会话存档快照已载入")
    else:
        print("[Sandbox] ⚠️ 未找到生产会话存档——本次以空会话运行")

    print(f"[Sandbox] user_data_dir = {sandbox}")
    return sandbox


def main() -> None:
    parser = argparse.ArgumentParser(description="生成质量评测（忠实度 / 拒答率）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全部）")
    parser.add_argument("--verbose", action="store_true", help="打印完整回答（人工校准用）")
    args = parser.parse_args()

    cases = load_cases(os.path.join("tests", "eval_data", "e2e_set.jsonl"))
    if args.limit:
        cases = cases[: args.limit]
    print("生成质量评测：%d 条样本（端到端完整链路）\n" % len(cases))

    # 隔离作用域：全部用户数据落在沙箱，绝不触碰生产存档与记忆库
    sandbox = _make_eval_sandbox()
    engine = RAGEngine(user_data_dir=sandbox)   # 完整引擎（含知识库、会话、工具）
    judge = LLMClient()

    cover_ok = faithful_ok = 0
    refuse_correct = 0
    refuse_n = 0            # 拒答判定的**有效分母**（排除链路异常样本）
    judged_n = 0
    fails = []
    chain_errors: list[tuple[int, str, str]] = []   # [(id, question, reason)]

    for case in cases:
        q = case["question"]
        should_refuse = case.get("should_refuse", False)
        t0 = time.time()

        # ① 跑完整生产链路，收集流式回答
        answer = ""
        chain_error = None
        try:
            for msg_type, chunk in engine.ask(q):
                if msg_type == "content":
                    answer += chunk
                elif msg_type == "error":
                    # LLMClient 在 API 失败时以 "error" 类型送出提示文案（2026-10-08 起）。
                    # 必须在这里捕获：它**不能**被当成模型的回答。
                    chain_error = chunk
        except Exception as e:
            chain_error = e
            print("  #%2d 链路异常: %s" % (case["id"], e))

        # ⚠️ 空回答检测 —— **评测方法缺陷之四修复，2026-10-08**
        #
        # 症状：余额耗尽（402 Insufficient Balance）时，20 条里 **9 条返回空回答**，
        # 但报告照常打印「拒答 90.0% / 覆盖 81.8% / 忠实 100.0%」，看起来完全正常。
        #
        # 根因有两层：
        #   1. `LLMClient` 对 API 失败是**吞掉异常**并返回空内容 —— 所以 `ask()` 正常结束，
        #      不抛异常，上面那个 `except` 永远不触发；
        #   2. 空回答随后被**静默计入指标**：
        #      · `is_refusal("") == False` → `should_refuse=False` 的样本被记为"拒答正确"
        #        → **拒答率虚高**
        #      · 质量评审被 `and answer` 跳过 → 分母悄悄缩小，覆盖/忠实看起来仍"正常"
        #
        # 修复：空回答一律判为**链路异常**，**不计入任何指标的分母**，并在汇总处高亮，
        # 使"跑了一半没余额"这种事故不可能再被误读成有效基线。
        if chain_error is None and not answer.strip():
            chain_error = "空回答（疑 API 失败/余额不足；异常被 LLMClient 吞掉）"
            print("  #%2d 链路异常: %s" % (case["id"], chain_error))

        elapsed = time.time() - t0

        if chain_error is not None:
            chain_errors.append((case["id"], q, str(chain_error)[:120]))
            print("  #%2d %-34s [无效样本，未计入指标] %.0fs" % (case["id"], q, elapsed))
            continue

        refused = is_refusal(answer)

        # ② 拒答判定（程序）
        refuse_ok = (refused == should_refuse)
        refuse_correct += refuse_ok
        refuse_n += 1

        # ③ 质量判定（judge；拒答样本跳过质量评审）
        covers = faithful = None
        if not should_refuse and answer:
            # ⚠️ 截断上限 6000 字（原为 2000）——**评测方法缺陷修复，2026-10-07**
            #
            # 症状：#4「风险分级+给建议」连续 4 次被判 covers=False（"未给出针对种植户的
            # 建议"），但人工核对回答发现它**明确含有**「## 五、给种植户的分区建议（重点
            # 段落）」共 4 个小节。原因：judge 只看得到 answer[:2000]，而该回答长 3400~3500
            # 字，**建议段落整个落在截断线之外**。
            #
            # 后果：**越完整的长回答越容易被冤枉**——这与本项目历史上"judge 上下文缺陷
            # 导致忠实度误判为 50%"是同一类问题（见《01_评测体系建设与报告》5.5）。
            # 教训：让 judge 判断"是否覆盖要点"，就必须让它看到**完整**回答。
            #
            # 6000 字的取舍：实测最长回答约 3500 字，6000 留出近一倍余量；
            # 仍设上限是为了防止异常超长回答把 judge 的上下文撑爆。
            verdict = parse_judge(judge.generate_response([{
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    today_date=date.today().isoformat(),
                    question=q,
                    key_points="、".join(case.get("key_points", [])),
                    answer=answer[:6000],
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
    invalid_n = len(chain_errors)
    print("\n" + "=" * 60)
    if invalid_n:
        # 有无效样本时**先高亮警告**，避免读者直接把下面的百分比当有效基线
        print("⚠️  本次运行不完整：%d/%d 条为**无效样本**（链路异常），已排除出全部分母。"
              % (invalid_n, total))
        print("⚠️  下面指标**不能作为基线**，请先补齐后再跑。")
        print("-" * 60)
        for cid, q, reason in chain_errors:
            print("    #%2d %s — %s" % (cid, q[:30], reason))
        print("-" * 60)
    print("拒答准确率: %d/%d = %.1f%%  （目标 ≥ 90%%）" %
          (refuse_correct, refuse_n, refuse_correct / refuse_n * 100 if refuse_n else 0.0))
    if judged_n:
        print("要点覆盖率: %d/%d = %.1f%%  （目标 ≥ 80%%）" %
              (cover_ok, judged_n, cover_ok / judged_n * 100))
        print("忠实度    : %d/%d = %.1f%%  （目标 ≥ 90%%）" %
              (faithful_ok, judged_n, faithful_ok / judged_n * 100))
    print("有效样本  : %d/%d（拒答判定分母 %d ｜ 质量判定分母 %d）"
          % (total - invalid_n, total, refuse_n, judged_n))
    if fails:
        print("\n质量未达标明细:")
        for cid, q, covers, faithful, reason in fails:
            print("  #%2d %s" % (cid, q[:40]))
            print("        covers=%s faithful=%s | %s" % (covers, faithful, reason[:80]))


if __name__ == "__main__":
    main()
