# -*- coding: utf-8 -*-
"""L4 冲突消解判定回放检查 —— 用真实失败样本验证 `L4_CONFLICT_PROMPT` 与动作解析。

【定位】与 `smoke_memory.py` 分工不同：
        · `smoke_memory.py`  验证"记忆**还活着**吗" —— 确定性、**零 LLM**
        · 本脚本            验证"冲突判定**准不准**" —— **需 LLM**、有随机性
        它是 E4 记忆评测集的前身，目前只覆盖「冲突消解」这一维。

【用例来源】全部取自**真实运行日志**，非人造样本：
        · 2026-10-04 桌面端实跑（短板 #19 的 +4 条膨胀）
        · 2026-10-05 S4 全量评测

【已知局限】冲突检查是 **1 对 1**（`search_l4_for_conflict` 只返回最相似的 1 条旧记忆），
        因此「派生事实」（"120亩"由"100亩"+"20亩"派生）这类需要**多路比较**的情形
        无法在本脚本覆盖 —— 需改成 top-K 候选后才能测。

【用法】python tests/check_l4_conflict.py [--repeat N]
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.llm_client import LLMClient                       # noqa: E402
from core.rag_engine import L4_CONFLICT_PROMPT, _parse_l4_decision  # noqa: E402

# (说明, 旧记忆, 新候选事实, 期望动作集合, 来源)
CASES = [
    ("近义改写（配种→配置种植）", "用户配种长绒棉", "用户配置种植长绒棉",
     {"NOOP"}, "10-04 桌面端实跑"),
    ("加限定词但信息未变", "用户10月中下旬机采", "用户长期目标是10月中下旬机采",
     {"NOOP"}, "10-04 桌面端实跑"),
    ("计划与陈述互转", "用户计划8月31日停水", "用户8月31日停水",
     {"NOOP", "UPDATE"}, "10-05 S4 评测"),
    ("近义改写（测产）", "用户计划9月底测产", "用户计划9月底进行测产",
     {"NOOP"}, "10-04 桌面端实跑"),
    ("近义改写（施肥）", "用户在花铃期随水滴施高钾水溶肥3—5公斤/亩",
     "用户花铃期追施高钾3—5公斤/次", {"NOOP", "UPDATE"}, "10-05 S4 评测"),
    ("真·品种更换（应 UPDATE）", "用户主栽塔河2号", "用户已改种新陆早77号",
     {"UPDATE"}, "构造对照"),
    ("真·面积变化（应 UPDATE）", "用户有100亩棉田", "用户棉田扩大到150亩",
     {"UPDATE"}, "构造对照"),
    ("真·不同属性（应 ADD）", "用户拥有20亩棉田", "用户偏好傍晚施药",
     {"ADD"}, "构造对照"),
    ("真·不同对象（应 ADD）", "用户的阿克苏地块主栽塔河2号",
     "用户的阿拉尔地块种植长绒棉", {"ADD"}, "构造对照"),
    ("真·计划取消（应 DELETE）", "用户计划10月售棉", "用户取消了售棉计划",
     {"DELETE", "UPDATE"}, "构造对照"),
]

# 动作解析的确定性用例（零 LLM）：旧实现用子串匹配，会把 "不应 ADD" 判成 ADD
PARSE_CASES = [
    ("纯单词", "UPDATE", "UPDATE"),
    ("带句号", "ADD.", "ADD"),
    ("带换行与空白", "  NOOP\n", "NOOP"),
    ("解释性文字（旧实现会判反）", "应 NOOP，不应 ADD", "NOOP"),
    ("英文解释", "I think NOOP is correct, not ADD", "NOOP"),
    ("无法识别", "我不确定", "NOOP"),
    ("空串", "", "NOOP"),
    ("None", None, "NOOP"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="L4 冲突消解判定回放检查")
    ap.add_argument("--repeat", type=int, default=1, help="重复次数（LLM 有随机性）")
    args = ap.parse_args()

    print("=" * 78)
    print("第 1 部分：动作解析（确定性，零 LLM）")
    print("=" * 78)
    p_ok = 0
    for desc, raw, exp in PARSE_CASES:
        got, _idx = _parse_l4_decision(raw)
        ok = got == exp
        p_ok += ok
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {desc:<28} {str(raw)[:26]!r:<30} → {got}  (期望 {exp})")
    print(f"  小计: {p_ok}/{len(PARSE_CASES)}")

    print()
    print("=" * 78)
    print(f"第 2 部分：冲突判定（需 LLM × {args.repeat} 次）")
    print("=" * 78)
    llm = LLMClient()

    tally: dict[str, list[bool]] = collections.defaultdict(list)
    total_ok = 0
    total_n = 0
    for rnd in range(args.repeat):
        if args.repeat > 1:
            print(f"\n  ── 第 {rnd + 1}/{args.repeat} 轮 ──")
        for desc, old, new, exp, src in CASES:
            # 本脚本只测 prompt 与解析，故用**单候选**形式喂入（等价于候选唯一的情形）；
            # 完整多候选链路由 tests/eval_memory.py 覆盖。
            prompt = L4_CONFLICT_PROMPT.format(candidates=f"1. {old}", new_fact=new)
            try:
                raw = llm.generate_response([{"role": "user", "content": prompt}])
                got, _idx = _parse_l4_decision(raw)
            except Exception as e:
                got = f"<异常 {type(e).__name__}>"
            ok = got in exp
            tally[desc].append(ok)
            total_ok += ok
            total_n += 1
            if args.repeat == 1 or not ok:
                mark = "OK  " if ok else "FAIL"
                print(f"  [{mark}] {desc:<26} → {got:<8} 期望 {'/'.join(sorted(exp)):<10} 〈{src}〉")

    print()
    print("=" * 78)
    print(f"冲突判定准确率: {total_ok}/{total_n} = {total_ok / total_n * 100:.1f}%")
    unstable = {k: v for k, v in tally.items() if not all(v)}
    if args.repeat > 1 and unstable:
        print("不稳定/失败样本:")
        for k, v in unstable.items():
            print(f"  · {k}  通过 {sum(v)}/{len(v)}")
    print(f"动作解析      : {p_ok}/{len(PARSE_CASES)}")
    print("=" * 78)
    return 0 if (p_ok == len(PARSE_CASES) and total_ok == total_n) else 1


if __name__ == "__main__":
    raise SystemExit(main())
