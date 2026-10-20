# -*- coding: utf-8 -*-
"""误差注入评测的守卫 —— 守"评测本身"是否正确。

【为什么评测也需要守卫】
        评测出错的后果比代码出错更隐蔽：**你会得到一个漂亮的假结论**。
        这个项目已经栽过一次（`covers` 指标阳性对照召回 0%，见 `01` 号 §5.6），
        所以对"新指标"的默认态度是：**先证明它能抓出错，再相信它**。

【本守卫的三层】

    ① **纯函数已知答案**：`decide` 的四档判据、`observe` 的平移语义、`covers` 的覆盖判定；
    ② **门禁本身必须能失败**（★ 最重要）：造 4 种"结论已失效"的假数据，
       断言 `check_gate` **逐个抓出来** —— 一个永不失败的门禁等于没有门禁；
    ③ **阳性对照**：注入必须真的改变结果（否则曲线是平的假数据）。

【用法】python tests/check_error_injection.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from eval_error_injection import (                             # noqa: E402
    ABOVE, ABSTAIN, BANDED, BELOW, COHORT, MAX_REL_WIDTH,
    NOMINAL_COVERAGE, THRESHOLD, TierMetrics, check_gate,
    coverage_breakdown, covers, decide, evaluate_tier, observe,
    reliability_threshold, run_sweep,
)


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


def _raises(fn) -> bool:
    """断言 fn 抛异常（用于「不得静默回落」这类反向断言）。"""
    try:
        fn()
    except Exception:                       # noqa: BLE001
        return True
    return False


def _p(lo: float, hi: float, count: float | None = None):
    from eval_error_injection import Perception
    c = count if count is not None else (lo + hi) / 2
    return Perception(field_id="T", count=c, lo=lo, hi=hi)


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：★ 四档决策的判据（只看区间与阈值，不看语言）")
    # ------------------------------------------------------------------
    c.check("区间下界 > 阈值 → ABOVE（可采收）",
            decide(_p(80, 100)) == ABOVE, decide(_p(80, 100)))
    c.check("区间上界 < 阈值 → BELOW（继续观察）",
            decide(_p(40, 60)) == BELOW, decide(_p(40, 60)))
    # 跨阈值 + 区间窄 → 降级表述（靠近阈值，可给"接近"的定性说法）
    c.check("★ 跨阈值 + 区间窄 → BANDED（降级表述）",
            decide(_p(74, 76, count=75)) == BANDED, decide(_p(74, 76, count=75)))
    # 跨阈值 + 区间宽 → 弃答（信息不足，建议补测）
    c.check("★★ 跨阈值 + 区间宽 → ABSTAIN（弃答/补测）",
            decide(_p(40, 110, count=75)) == ABSTAIN, decide(_p(40, 110, count=75)))
    c.check("★ 边界：下界恰好等于阈值 → 不算 ABOVE（要严格大于）",
            decide(_p(75, 90)) != ABOVE, decide(_p(75, 90)))
    c.check("阈值可配（换阈值，判据跟着变）",
            decide(_p(80, 100), threshold=95) != ABOVE)
    # ★ 用 MAX_REL_WIDTH 的**真实边界**测一次：恰好窄于上限 → 降级，恰好宽于 → 弃答
    half_ok = MAX_REL_WIDTH / 2.05      # 略窄于上限 → 应 BANDED
    half_bad = MAX_REL_WIDTH / 1.95     # 略宽于上限 → 应 ABSTAIN
    c.check(f"★ MAX_REL_WIDTH={MAX_REL_WIDTH:.0%} 的边界：略窄→BANDED",
            decide(_p(75 * (1 - half_ok), 75 * (1 + half_ok), count=75)) == BANDED,
            decide(_p(75 * (1 - half_ok), 75 * (1 + half_ok), count=75)))
    c.check(f"★ MAX_REL_WIDTH={MAX_REL_WIDTH:.0%} 的边界：略宽→ABSTAIN",
            decide(_p(75 * (1 - half_bad), 75 * (1 + half_bad), count=75)) == ABSTAIN,
            decide(_p(75 * (1 - half_bad), 75 * (1 + half_bad), count=75)))
    c.check("区间宽度上限可配（放宽后原本弃答的变成降级）",
            decide(_p(40, 110, count=75), max_rel_width=5.0) == BANDED)

    # ------------------------------------------------------------------
    c.section("第 2 节：★★ 注入语义 —— 平移，不是变宽")
    # ------------------------------------------------------------------
    base = observe(80.0, 0.0, rel_half=0.05, field_id="X1")
    wider = observe(80.0, 0.0, rel_half=0.20, field_id="X1")
    c.check("★ 半宽变大 → 区间真的变宽",
            (wider.hi - wider.lo) > (base.hi - base.lo))
    # 关键：注入误差**不改变相对宽度**
    e0 = observe(80.0, 0.0, rel_half=0.10, field_id="X2")
    e20 = observe(80.0, 0.20, rel_half=0.10, field_id="X2")
    w0 = (e0.hi - e0.lo) / e0.count
    w20 = (e20.hi - e20.lo) / e20.count
    c.check("★★ 注入 20% 误差后，**相对宽度不变**（误差是平移不是变宽）",
            abs(w0 - w20) < 1e-9, f"{w0:.4f} vs {w20:.4f}")
    c.check("★ 点估计被平移了约 20%",
            abs(e20.count / e0.count - 1.2) < 1e-9,
            f"{e20.count / e0.count:.4f}")
    c.check("★★ 于是大误差下区间**不再覆盖真值**（校准失效的机制）",
            covers(e0, 80.0) and not covers(observe(80.0, 0.5, rel_half=0.05,
                                                   field_id="X3"), 80.0))

    # 可复现性 —— 同一 field_id 每次一样（否则"曲线变了"分不清真假）
    a = observe(80.0, 0.0, rel_half=0.05, field_id="REPRO")
    b = observe(80.0, 0.0, rel_half=0.05, field_id="REPRO")
    c.check("★★ 同一块田重复观测结果**完全一致**（可复现）",
            (a.lo, a.hi) == (b.lo, b.hi), f"{a.lo:.4f} vs {b.lo:.4f}")
    c.check("★ 不同田块结果不同（固有误差是逐田的）",
            observe(80.0, 0.0, rel_half=0.05, field_id="R1").lo
            != observe(80.0, 0.0, rel_half=0.05, field_id="R2").lo)

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 队列设计约束（不满足则指标分母为 0）")
    # ------------------------------------------------------------------
    tiers = run_sweep()
    base_tier = tiers[0]
    c.check("★ 基准 ε=0 时三种实质档位都存在（分母不为 0）",
            base_tier.counts.get(ABOVE, 0) > 0 and base_tier.counts.get(BELOW, 0) > 0,
            str(base_tier.counts))
    c.check("★★ 基准 ε=0 时**存在该弃答的田块**（弃答召回的分母）",
            base_tier.abstain_recall is not None, str(base_tier.abstain_recall))
    c.check("★★ 基准 ε=0 时**存在不该弃答的田块**（弃答特异的分母）",
            base_tier.abstain_specificity is not None, str(base_tier.abstain_specificity))
    c.check("★ 弃答比例在 10%–50% 之间（太高=系统什么都不答，不现实）",
            0.10 <= 1 - base_tier.answer_coverage <= 0.50,
            f"{1 - base_tier.answer_coverage:.0%}")
    c.check("★ 队列围绕阈值对称（防把队列偏斜误读成规则属性）",
            abs(sum(r["truth"] - THRESHOLD for r in COHORT
                    if r["rel_half"] <= 0.10)) < 30,
            str(sum(r["truth"] - THRESHOLD for r in COHORT if r["rel_half"] <= 0.10)))

    # ------------------------------------------------------------------
    c.section("第 4 节：★★★ 门禁本身必须能失败（阳性对照）")
    # ------------------------------------------------------------------
    # 真数据应通过
    c.check("★★ 真评测数据 → 门禁**通过**（没有违规）",
            check_gate(tiers) == [], str(check_gate(tiers))[:100])

    # 造 ① 基准不可信：ε=0 覆盖率远低于名义
    bad_cov = [TierMetrics(eps=0.0, coverage_rate=0.30, misjudge_rate=0.0),
               TierMetrics(eps=0.20, coverage_rate=0.10, misjudge_rate=0.30)]
    v1 = check_gate(bad_cov)
    c.check("★★ 造「基准覆盖率 30%」→ 门禁**抓到**",
            any("①" in x for x in v1), str(v1)[:90])

    # 造 ② 基准误判率不为 0
    bad_base = [TierMetrics(eps=0.0, coverage_rate=0.95, misjudge_rate=0.2),
                TierMetrics(eps=0.20, coverage_rate=0.5, misjudge_rate=0.3)]
    v2 = check_gate(bad_base)
    c.check("★★ 造「ε=0 误判率 20%」→ 门禁**抓到**",
            any("②" in x for x in v2), str(v2)[:90])

    # 造 ③ 无可用区（小误差就崩）
    bad_usable = [TierMetrics(eps=0.0, coverage_rate=0.95, misjudge_rate=0.0),
                  TierMetrics(eps=0.05, coverage_rate=0.9, misjudge_rate=0.50),
                  TierMetrics(eps=0.10, coverage_rate=0.9, misjudge_rate=0.60)]
    v3 = check_gate(bad_usable)
    c.check("★★ 造「5% 误差就误判 50%」→ 门禁**抓到**（无可用区）",
            any("③" in x for x in v3), str(v3)[:90])

    # 造 ④ 注入没生效（大误差下误判不涨）
    bad_inject = [TierMetrics(eps=0.0, coverage_rate=0.95, misjudge_rate=0.0),
                  TierMetrics(eps=0.40, coverage_rate=0.95, misjudge_rate=0.0)]
    v4 = check_gate(bad_inject)
    c.check("★★★ 造「40% 误差但误判仍为 0」→ 门禁**抓到**（注入没生效）",
            any("④" in x for x in v4), str(v4)[:90])

    c.check("★ 空数据 → 门禁**不通过**（不是默认放行）",
            check_gate([]) != [])

    # ------------------------------------------------------------------
    c.section("第 5 节：★ 阈值定位函数的边界")
    # ------------------------------------------------------------------
    mk = lambda e, m: TierMetrics(eps=e, coverage_rate=1.0, misjudge_rate=m)  # noqa: E731
    c.check("★ 首次超过就返回（不是返回最大的那个）",
            reliability_threshold([mk(0, 0.0), mk(0.05, 0.05), mk(0.10, 0.15),
                                   mk(0.20, 0.40)], limit=0.10) == 0.10)
    c.check("★ 恰好等于门限**不算**超过", 
            reliability_threshold([mk(0, 0.0), mk(0.1, 0.10)], limit=0.10) is None)
    c.check("★★ 全程没超过 → 返回 None（而不是编一个数）",
            reliability_threshold([mk(0, 0.0), mk(0.1, 0.01)], limit=0.10) is None)
    c.check("校准失效点同理（跌破才返回）",
            coverage_breakdown([TierMetrics(eps=0, coverage_rate=0.95),
                                TierMetrics(eps=0.1, coverage_rate=0.5)],
                               limit=0.10) == 0.1)

    # ------------------------------------------------------------------
    c.section("第 6 节：★ 指标方向必须正确（否则结论会反）")
    # ------------------------------------------------------------------
    # ★ 网格是**有符号**的（域偏移可正可负），所以单调性要**正负两半分别查**。
    #   直接对整条序列断言单调会误报 —— 守卫自己抓到过一次。
    pos = [t for t in tiers if t.eps >= -1e-9]
    neg = [t for t in tiers if t.eps <= 1e-9]
    c.check("★★ 正误差越大，误判率**不下降**",
            all(pos[i].misjudge_rate <= pos[i + 1].misjudge_rate + 1e-9
                for i in range(len(pos) - 1)),
            str([round(t.misjudge_rate, 3) for t in pos]))
    c.check("★★ 负误差越大（绝对值），误判率**不下降**",
            all(neg[i].misjudge_rate <= neg[i + 1].misjudge_rate + 1e-9
                for i in range(len(neg) - 1)),
            str([(round(t.eps, 2), round(t.misjudge_rate, 3)) for t in neg]))
    c.check("★★ 正误差越大，覆盖率**不上升**",
            all(pos[i].coverage_rate >= pos[i + 1].coverage_rate - 1e-9
                for i in range(len(pos) - 1)),
            str([round(t.coverage_rate, 3) for t in pos]))
    c.check("★★ 负误差越大（绝对值），覆盖率**不上升**",
            all(neg[i].coverage_rate >= neg[i + 1].coverage_rate - 1e-9
                for i in range(len(neg) - 1)),
            str([round(t.coverage_rate, 3) for t in neg]))
    c.check("★ ε=0 的覆盖率不低于名义值太多（基准可用）",
            tiers[0].coverage_rate >= NOMINAL_COVERAGE - 0.10,
            f"{tiers[0].coverage_rate:.0%}")
    c.check("★ 大误差下弃答召回下降（该弃的时候反而没弃 = 过度自信）",
            (tiers[-1].abstain_recall or 0) < (tiers[0].abstain_recall or 1),
            f"{tiers[0].abstain_recall} → {tiers[-1].abstain_recall}")

    # ------------------------------------------------------------------
    c.section("第 6.5 节：★★ 误差源可插拔（合成 ↔ 实测）")
    # ------------------------------------------------------------------
    # 【为什么这一节重要】论文 SQ2 要用**实测**误差分布，而现在用的是合成 ε 网格。
    # 如果换源需要改指标/曲线/门禁，那"等真数据来了就接"就是一句空话。
    # 所以断言：**换源只改一个字符串**。
    from eval_error_injection import (ERROR_SOURCES, EmpiricalErrorSource,
                                      SyntheticErrorSource, make_source)
    c.check("★ 两种误差源都已登记", set(ERROR_SOURCES) == {"synthetic", "empirical"},
            str(sorted(ERROR_SOURCES)))
    # ★ 直接断言「清单 ↔ 工厂 ↔ 类」三者一致（防将来新增源时漏登记某一处）
    c.check("★★ 登记表里的类**就是**那两个类（不是同名替身）",
            ERROR_SOURCES["synthetic"] is SyntheticErrorSource
            and ERROR_SOURCES["empirical"] is EmpiricalErrorSource)
    c.check("★ 每个源都带 name 属性（日志可追溯是哪个源）",
            all(getattr(cls, "name", None) for cls in ERROR_SOURCES.values()),
            str([getattr(c_, "name", None) for c_ in ERROR_SOURCES.values()]))
    c.check("★ 未登记的源**报错而非静默回落**", _raises(lambda: make_source("瞎写")))
    c.check("★ synthetic 能造出观测",
            make_source("synthetic", eps=0.2, rel_half=0.05, field_id="A",
                        truth=80.0).observe().count > 80)
    c.check("★★ empirical 也能造出观测（接口真的可用）",
            make_source("empirical", bias=-0.25, spread=0.1, rel_half=0.08,
                        field_id="A", truth=80.0).observe().count < 80)
    c.check("★ empirical 缺 bias/spread 时有默认值（换源不必先改队列）",
            make_source("empirical", rel_half=0.08, field_id="A",
                        truth=80.0).observe().count > 0)

    # ★★ 最关键：同一个调用形状，只换字符串
    ms = evaluate_tier(0.2, source_kind="synthetic")
    me = evaluate_tier(0.2, source_kind="empirical")
    c.check("★★ 换源后 evaluate_tier **不需要改调用方式**", True)
    c.check("★★ 两种源的指标都是合法的 TierMetrics",
            hasattr(ms, "misjudge_rate") and hasattr(me, "misjudge_rate"))
    c.check("★★ 两种源给出**不同**的曲线（说明源真的生效了，不是空转）",
            ms.coverage_rate != me.coverage_rate,
            f"{ms.coverage_rate:.2f} vs {me.coverage_rate:.2f}")

    # 基准恒用合成源的 eps=0（参照系不该随源变化）
    c.check("★ 基准（refs）不随 source_kind 变化",
            ms.details[0]["ref"] == me.details[0]["ref"],
            f'{ms.details[0]["ref"]} vs {me.details[0]["ref"]}')

    # ------------------------------------------------------------------
    c.section("第 7 节：★ 逐田详情可复核")
    # ------------------------------------------------------------------
    one = evaluate_tier(0.0)
    c.check("★ 每块田都有明细（能逐条复核）", len(one.details) == len(COHORT))
    c.check("★ 明细含真值 / 点估计 / 区间 / 基准档 / 实际档",
            set(one.details[0]) >= {"field", "truth", "point", "interval", "ref", "got"})
    c.check("★ 基准档 == 实际档（ε=0 时自己跟自己比）",
            all(d["ref"] == d["got"] for d in one.details))
    c.check("★ ε=0 时覆盖标记全为 True（真值在区间内）",
            all(d["covered"] for d in one.details))

    print()
    print("=" * 78)
    print(f"误差注入评测守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())