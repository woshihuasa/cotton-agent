# -*- coding: utf-8 -*-
"""不确定性门控守卫（U2）—— 判据 + 硬约束 + 越权审计 + 编排接线。

【定位】`docs/ROADMAP.md` 的 **U2**（不确定性门控 + 四档动作 + LLM 不可越权硬门）。

【最重要的两组断言】

    ① **Fail-Closed**：区间缺失 / 反向 / 阈值非法 → 一律 `ESCALATE`。
       "无法判断"必须落到**最保守**的档，而不是放行。

    ② **硬门的另一半必须存在**：只把约束写进提示词是**不够的** ——
       模型可以不听。所以断言 `audit()` 真的能抓出越权输出，
       并同时断言它**不会把守规矩的回答判成越权**（误报会让人关掉门控）。

【用法】python tests/check_uncertainty.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import uncertainty as U                            # noqa: E402
from core.orchestration import catalog, engine, loader       # noqa: E402
from core.orchestration.model import StepState               # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def ev(**kw):
    return U.evaluate(**kw)


def main() -> int:
    c = Checker()
    T = 75.0

    # ------------------------------------------------------------------
    c.section("第 1 节：★★ 四档判据（位置看区间，宽度看相对值）")
    # ------------------------------------------------------------------
    c.check("窄 + 整体高于 → ADVISE（可正常建议）",
            ev(lo=80, hi=86, value=83, threshold=T).level is U.Level.ADVISE,
            ev(lo=80, hi=86, value=83, threshold=T).level.value)
    c.check("★ 宽 + 整体高于 → ADVISE_WITH_INTERVAL（给建议但必须带区间）",
            ev(lo=80, hi=140, value=110, threshold=T).level
            is U.Level.ADVISE_WITH_INTERVAL,
            ev(lo=80, hi=140, value=110, threshold=T).level.value)
    c.check("窄 + 整体低于 → ADVISE（明确不可采收，也是正常建议）",
            ev(lo=48, hi=52, value=50, threshold=T).level is U.Level.ADVISE,
            ev(lo=48, hi=52, value=50, threshold=T).level.value)
    c.check("宽 + 整体低于 → ADVISE_WITH_INTERVAL",
            ev(lo=30, hi=60, value=45, threshold=T).level
            is U.Level.ADVISE_WITH_INTERVAL)
    c.check("★★ 跨阈值 + 窄 → QUALITATIVE（只能定性）",
            ev(lo=74, hi=76, value=75, threshold=T).level is U.Level.QUALITATIVE,
            ev(lo=74, hi=76, value=75, threshold=T).level.value)
    c.check("★★ 跨阈值 + 宽 → ESCALATE（弃答/补测）",
            ev(lo=40, hi=110, value=75, threshold=T).level is U.Level.ESCALATE,
            ev(lo=40, hi=110, value=75, threshold=T).level.value)

    c.check("★ side 与 level 是两件事：ABOVE 也可能因太宽而降档",
            ev(lo=80, hi=140, value=110, threshold=T).side is U.Side.ABOVE
            and ev(lo=80, hi=140, value=110, threshold=T).level
            is U.Level.ADVISE_WITH_INTERVAL)
    c.check("★ 边界：下界恰好等于阈值 → **不算高于**（要严格大于）",
            ev(lo=75, hi=90, value=82, threshold=T).side is U.Side.STRADDLE,
            ev(lo=75, hi=90, value=82, threshold=T).side.value)
    c.check("★ 边界：上界恰好等于阈值 → **不算低于**",
            ev(lo=50, hi=75, value=62, threshold=T).side is U.Side.STRADDLE)
    c.check("★ 宽度上限可配：放宽后宽区间也能 ADVISE",
            ev(lo=80, hi=140, value=110, threshold=T, max_rel_width=1.0).level
            is U.Level.ADVISE)

    # ------------------------------------------------------------------
    c.section("第 2 节：★★★ Fail-Closed —— 无法判断一律 ESCALATE")
    # ------------------------------------------------------------------
    hard = [
        ("无区间（lo/hi 都是 None）", dict(lo=None, hi=None, threshold=T)),
        ("只有下界", dict(lo=80, hi=None, threshold=T)),
        ("只有上界", dict(lo=None, hi=90, threshold=T)),
        ("区间反向（lo>hi）", dict(lo=90, hi=80, threshold=T)),
        ("阈值非法（0）", dict(lo=80, hi=90, threshold=0)),
        ("阈值为负", dict(lo=80, hi=90, threshold=-5)),
        ("区间上限非正", dict(lo=-50, hi=0, threshold=T)),
        ("区间不是数字", dict(lo="x", hi="y", threshold=T)),
    ]
    for name, kw in hard:
        d = ev(**kw)
        c.check(f"★★ {name} → **ESCALATE**", d.level is U.Level.ESCALATE,
                d.level.value)

    d_hard = ev(lo=None, hi=None, threshold=T)
    c.check("★ 硬失败时 side = UNKNOWN（可区分「没有区间」和「区间跨阈值」）",
            d_hard.side is U.Side.UNKNOWN, d_hard.side.value)
    c.check("★ 硬失败时 reason 说清原因", bool(d_hard.reason))
    c.check("★★ 硬失败时 must_abstain 为 True（下游据此硬拦）", d_hard.must_abstain)

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 前置硬约束文本")
    # ------------------------------------------------------------------
    d_adv = ev(lo=48, hi=52, value=50, threshold=T)
    txt_adv = U.constraint_text(d_adv)
    c.check("★ 文本里写明档位", "ADVISE" in txt_adv)
    c.check("★ 文本里列出允许的动作", "允许" in txt_adv)
    c.check("★ 文本里有「不得」段（哪怕该档没有禁止项时也应结构完整）"
            , "不得" in txt_adv or not d_adv.forbidden)

    d_esc = ev(lo=40, hi=110, value=75, threshold=T)
    txt_esc = U.constraint_text(d_esc)
    c.check("★★ ESCALATE 的约束文本明确要求弃答", "弃答" in txt_esc)
    c.check("★★ 且点明不许给具体农事建议", "不要" in txt_esc and "建议" in txt_esc)
    c.check("★ ESCALATE 文本列出禁止动作", "不得" in txt_esc)

    d_q = ev(lo=74, hi=76, value=75, threshold=T)
    txt_q = U.constraint_text(d_q)
    c.check("★★ QUALITATIVE 文本要求只能定性", "定性" in txt_q)
    c.check("★ QUALITATIVE 文本要求不下确定结论", "确定结论" in txt_q)

    d_awi = ev(lo=80, hi=140, value=110, threshold=T)
    txt_awi = U.constraint_text(d_awi)
    c.check("★★ ADVISE_WITH_INTERVAL 文本**要求写出区间**", "必须同时写出区间" in txt_awi)
    c.check("★ 且把具体上下界写进去了",
            "80" in txt_awi and "140" in txt_awi, txt_awi[:100])

    # ------------------------------------------------------------------
    c.section("第 4 节：★★ 后置审计 —— 必须抓得住越权")
    # ------------------------------------------------------------------
    bad_esc = [
        "建议采收，亩产 125 公斤左右。",
        "可以采收，没问题。",
        "放心，产量为 130 公斤。",
        "确定可以收了。",
    ]
    for t in bad_esc:
        a = U.audit(t, d_esc)
        c.check(f"★★ ESCALATE 档抓出越权: {t[:16]!r}…", not a.ok, str(a))

    bad_q = ["可以采收，非常好。", "肯定能收。", "没有问题。"]
    for t in bad_q:
        c.check(f"★★ QUALITATIVE 档抓出越权: {t[:14]!r}…",
                not U.audit(t, d_q).ok, str(U.audit(t, d_q)))

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ 审计不得误伤守规矩的回答（否则人会关掉门控）")
    # ------------------------------------------------------------------
    good_esc = [
        "本次检测区间为 [40, 110]，跨越了 75 的阈值，信息不足，建议补飞或人工踏查。",
        "抱歉，当前影像质量不足以给出可靠结论，建议重新航拍后再评估。",
        "缺少年份一致的样方数据，无法判断这块地是否达到采收标准。",
    ]
    for t in good_esc:
        c.check(f"★★ 守规矩的 ESCALATE 回答**不误判**: {t[:16]!r}…",
                U.audit(t, d_esc).ok, str(U.audit(t, d_esc)))

    good_q = [
        "该地块密度接近阈值（区间 [74, 76] 含 75），无法给出确定结论，建议人工确认。",
        "从区间看大致处于临界位置，需要人工复核。",
    ]
    for t in good_q:
        c.check(f"★ 守规矩的 QUALITATIVE 回答不误判: {t[:16]!r}…",
                U.audit(t, d_q).ok, str(U.audit(t, d_q)))

    c.check("★ ADVISE 档不设词面禁忌（正常回答不该被拦）",
            U.audit("建议采收，亩产 125 公斤。", d_adv).ok)
    c.check("★ 空回答不算越权（那是「没答」，由别的机制处理）",
            U.audit("", d_esc).ok)
    c.check("★ 非字符串不抛异常", U.audit(None, d_esc).ok)

    # ------------------------------------------------------------------
    c.section("第 6 节：★★ 编排层接线 —— 门禁真的能拦")
    # ------------------------------------------------------------------
    spec = catalog.get("gate.uncertainty")
    c.check("★ `gate.uncertainty` 在目录里", spec is not None)
    c.check("★ 在 EXECUTABLE_TYPES 里", "gate.uncertainty" in catalog.EXECUTABLE_TYPES)
    c.check("★ 副作用为 none（纯计算，可重试）",
            spec is not None and spec.side_effect.value == "none",
            spec.side_effect.value if spec else "None")

    def run_yaml(text: str):
        wf = loader.load_text(text)
        return engine.Engine(wf, persist=False).run()

    r = run_yaml("""
id: u_ok
nodes:
  - { id: chk, type: gate.uncertainty, params: { lo: 80, hi: 86, value: 83, threshold: 75 }, side_effect: none }
""")
    c.check("★★ 窄+高于 → 放行（done）", r.state == StepState.DONE, r.state.value)
    c.check("★ 产出里带 level / side / reason / constraint",
            set(r.steps["chk"].output or {}) >= {"level", "side", "reason", "constraint"},
            str(sorted(r.steps["chk"].output or {})))

    r = run_yaml("""
id: u_abstain
nodes:
  - { id: chk, type: gate.uncertainty, params: { lo: 40, hi: 110, value: 75, threshold: 75 }, side_effect: none }
""")
    c.check("★★ 宽+跨阈值 → **拦下**（failed）", r.steps["chk"].state == StepState.FAILED)
    c.check("★ 错误信息说明了原因", "跨越" in r.steps["chk"].error,
            r.steps["chk"].error[:60])

    r = run_yaml("""
id: u_level
nodes:
  - { id: chk, type: gate.uncertainty, params: { lo: 74, hi: 76, value: 75, threshold: 75, level: ADVISE }, side_effect: none }
""")
    c.check("★★ 档位不足（QUALITATIVE < 要求 ADVISE）→ 拦下",
            r.steps["chk"].state == StepState.FAILED)
    c.check("★ 错误信息点名实际档位与要求档位",
            "QUALITATIVE" in r.steps["chk"].error and "ADVISE" in r.steps["chk"].error,
            r.steps["chk"].error[:80])

    r = run_yaml("""
id: u_nolevel
nodes:
  - { id: chk, type: gate.uncertainty, params: { lo: 74, hi: 76, value: 75, threshold: 75 }, side_effect: none }
""")
    c.check("★★ 不写 level → **只判不拦**（门禁有两种用法，不给选择只能二选一）",
            r.state == StepState.DONE, r.state.value)

    r = run_yaml("""
id: u_badlevel
nodes:
  - { id: chk, type: gate.uncertainty, params: { lo: 80, hi: 86, value: 83, threshold: 75, level: 瞎写 }, side_effect: none }
""")
    c.check("★ 未知档位名 → 拒绝（不静默忽略）",
            r.steps["chk"].state == StepState.FAILED)
    c.check("★ 且列出可选档位", "ESCALATE" in r.steps["chk"].error,
            r.steps["chk"].error[:80])

    # ESCALATE 不能被 level 参数绕过
    r = run_yaml("""
id: u_bypass
nodes:
  - { id: chk, type: gate.uncertainty, params: { lo: 40, hi: 110, value: 75, threshold: 75, level: ADVISE }, side_effect: none }
""")
    c.check("★★★ 弃答**不可被 `level` 参数绕过**（硬要求）",
            r.steps["chk"].state == StepState.FAILED)

    # ------------------------------------------------------------------
    c.section("第 7 节：★ 与 U1 评测用的是**同一条规则**（防两份地图）")
    # ------------------------------------------------------------------
    from eval_error_injection import (ABOVE, ABSTAIN, BANDED, BELOW,
                                      decide)
    from eval_error_injection import Perception
    c.check("★★ U1 的 decide() 委派给 core.uncertainty（源码里有 import）",
            "uncertainty" in (ROOT / "tests" / "eval_error_injection.py")
            .read_text(encoding="utf-8"))
    p_above = Perception("T", count=83, lo=80, hi=86)
    p_below = Perception("T", count=50, lo=48, hi=52)
    p_straddle_narrow = Perception("T", count=75, lo=74, hi=76)
    p_straddle_wide = Perception("T", count=75, lo=40, hi=110)
    c.check("★ 两边对「高于」的判定一致", decide(p_above) == ABOVE, decide(p_above))
    c.check("★ 两边对「低于」的判定一致", decide(p_below) == BELOW, decide(p_below))
    c.check("★ 两边对「跨阈值+窄」的判定一致",
            decide(p_straddle_narrow) == BANDED, decide(p_straddle_narrow))
    c.check("★ 两边对「跨阈值+宽」的判定一致",
            decide(p_straddle_wide) == ABSTAIN, decide(p_straddle_wide))

    print()
    print("=" * 78)
    print(f"不确定性门控守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())