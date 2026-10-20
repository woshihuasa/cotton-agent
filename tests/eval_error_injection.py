# -*- coding: utf-8 -*-
"""第六类评测集 · **感知误差注入**（U1）—— 零 LLM、零外部数据。

【它测什么，不测什么】（这句话决定了本文件全部设计）

    ✗ **不测**检测器准不准（那是 U4 的事，需要真 YOLO 与真影像）
    ✓ **测**「**决策规则对感知误差有多敏感**」

    所以注入的误差是**合成的**，**不需要任何真实数据** ——
    这正好对上「零外部依赖」：等真检测器接进来，只换注入源，指标与门禁不用改。

【核心机制：误差是"平移"，不是"变宽"】

    域偏移下的检测器不是"变得不确定"，而是「**仍然很自信，但值偏了**」——
    区间宽度不变，中心点整体平移。**后果是区间不再覆盖真值（覆盖率掉），
    而这条链最终表现为决策错误。**

    ★ 这与"把区间调宽"完全不同：调宽会让系统更容易弃答（看似安全），
    平移则会让它**自信地答错**。**后者才是真实风险**，也是本文要测的东西。

【四档参考决策】（U2 会把它变成生产门控；此处先作为**评测基准**）

    区间下界 > 阈值           → `ABOVE`    可采收（正常建议）
    区间上界 < 阈值           → `BELOW`    继续观察
    跨阈值 + 区间窄           → `BANDED`   **降级为定性表述**（靠近阈值，建议人工确认）
    跨阈值 + 区间宽           → `ABSTAIN`  **信息不足**（建议补飞 / 人工踏查）

    ★ **弃答的触发信号是「区间宽窄」，不是拍脑袋的阈值** ——
    这是本文创新点 2 的落点：让弃答由**校准区间**触发，而不是由语言内部信号触发。

【产出】

    · **覆盖率曲线**：ε ↑ → 区间仍覆盖真值的比例 ↓（校准失效）
    · **决策退化曲线**：ε ↑ → 误判率 ↑
    · **弃答准确率（双向）**：该弃答时弃 / 不该弃时没弃
    · **可靠性阈值**：误判率首次超过上限的那个 ε
    · **🆕 门禁（本文第 43 项）**：不达标 **真的返回非零退出码**，而不只是打印数字

【用法】python tests/eval_error_injection.py [--limit-misjudge 0.10] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import uncertainty  # noqa: E402

# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

#: 农艺阈值（个/㎡）：高于它 = 可采收。★ 换成任何别的值，**曲线形状不变**（见第 23 项敏感性）。
THRESHOLD = 75.0

#: 名义覆盖率（校准程序的属性，不是数据的属性）。
NOMINAL_COVERAGE = 0.90

#: 判"区间宽到没法用"的相对宽度上限。
#: 跨阈值时：区间相对宽度 ≤ 它 → 还能给**降级表述**；> 它 → **弃答（补测）**。
MAX_REL_WIDTH = 0.30

#: 误差注入网格（相对点估计）。★ 含 0 是为了拿"无误差基准"。
#: 误差注入网格（**有符号**：域偏移可正可负，所以两边都要扫 —— 曲线应呈 V 形）。
EPS_GRID = (0.0, 0.05, 0.10, 0.20, 0.30, 0.40,
            -0.05, -0.10, -0.20, -0.30, -0.40)

#: 决策档位
ABOVE = "ABOVE"      #: 可采收
BELOW = "BELOW"      #: 继续观察
BANDED = "BANDED"    #: 降级表述（跨阈值但区间够窄）
ABSTAIN = "ABSTAIN"  #: 弃答（信息不足，建议补测）

#: "作出实质决策"的档位（用于算作答覆盖率）
ANSWERING = frozenset({ABOVE, BELOW, BANDED})


@dataclass(frozen=True)
class Perception:
    """一次感知输出 —— **带区间**的计数。

    字段刻意对齐「不确定性契约」的雏形：点估计 / 区间 / 名义覆盖率 / 校准域。
    """

    field_id: str
    count: float          #: 点估计（个/㎡）
    lo: float             #: 区间下界
    hi: float             #: 区间上界
    coverage: float = NOMINAL_COVERAGE
    calibration_domain: str = "2025-10 3-5m Mavic3M"   #: 校准域标识
    eps: float = 0.0      #: 本次注入的误差比例（仅评测可见）


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------


def _field_residual(field_id: str) -> float:
    """**检测器固有误差**的确定性系数（同一块田每次一样 → 结果可复现）。

    返回 `[-1.1, 1.1]`，含义是「半个区间宽的多少倍」。

    ★ 为什么需要它：如果没有固有误差，`eps=0` 时真值恰好落在区间正中，
      覆盖率恒为 100% —— 那是个**平凡结果**，会让覆盖率曲线变成一条无意义的直线。
      加了这个，`eps=0` 的覆盖率才会落在名义值（90%）附近，后面跌破才有意义。

    ★ 为什么用哈希而不是 `random`：**评测必须可复现**。
      同一批田块重复跑要得到同一个数，否则"曲线变了"分不清是真变化还是随机抖动。
    """
    import hashlib
    h = int(hashlib.sha256(field_id.encode("utf-8")).hexdigest()[:8], 16)
    u = (h % 10000) / 10000.0            # [0, 1)
    return (u * 2.0 - 1.0) * 1.1         # [-1.1, 1.1]


def observe(truth: float, eps: float, *, rel_half: float,
            field_id: str = "", coverage: float = NOMINAL_COVERAGE,
            calibration_domain: str = "2025-10 3-5m Mavic3M") -> Perception:
    """把真值注入误差，得到一次"感知"。

    `eps=0` 时是**校准良好的感知**；`eps≠0` 时整个区间**平移** `truth*eps`。

    ★ **区间宽度不随 eps 变化** —— 这是有意的（见模块注释）：
    域偏移让值偏，不让模型知道自己偏。
    """
    base = 1.0 + _field_residual(field_id) * rel_half      # 固有误差（相对）
    center = truth * (1.0 + eps) * base
    half = abs(center) * rel_half
    return Perception(
        field_id=field_id, count=center,
        lo=center - half, hi=center + half,
        coverage=coverage, calibration_domain=calibration_domain, eps=eps,
    )


def covers(p: Perception, truth: float) -> bool:
    """区间是否覆盖真值 —— 覆盖率曲线的原子判定。"""
    return p.lo <= truth <= p.hi


# ---------------------------------------------------------------------------
# ★ 误差源（可插拔）—— 合成 ↔ 实测
# ---------------------------------------------------------------------------
#
# 【为什么要把"误差从哪来"抽出来】
#
#   本评测要回答的问题是固定的：**"决策规则对感知误差有多敏感"**。
#   而"误差长什么样"是**可以换的**：
#
#     · **现在**：合成误差（ε 网格，平移一个比例）—— 用来把规则、指标、曲线、
#       门禁全部建好并测透。**不需要真实数据**。
#     · **将来**：实测误差（检测器 vs 样方的残差分布）—— 论文 SQ2 要的就是这个。
#
#   ★ 关键：**换误差源不应该动指标、曲线、门禁任何一行。**
#     所以这里把"误差怎么产生"从 `evaluate_tier` 里抽出来，变成可替换的对象。
#
#   这正是 U1 当时写下的那句话的兑现：
#     「等真检测器接进来，**只换注入源，指标与门禁不用改**。」
#
# 【实测源长什么样（接口已留，待真实检测器输出接入）】
#
#   · **乘性偏置** `bias`：NMS 少计是**乘性**的（密度越高漏越多），
#     所以 `bias = median(log(检测) - log(真值))`
#   · **随机散度** `spread`：扣掉偏置后的波动
#   → log 空间里乘性变加性，标准共形可直接套
#     （即 `Kwon&Kim精读` 建议的那个变换）。


class SyntheticErrorSource:
    """**合成误差源**：按给定的 ε 统一平移。

    ★ 价值：**能提前把规则建好**（不需要真实数据）。
    ★ 局限：**误差形态是假设的**（单一比例平移），不是实测分布。
    """

    name = "synthetic"

    def __init__(self, eps: float, *, rel_half: float, field_id: str,
                 truth: float) -> None:
        self.eps = eps
        self.rel_half = rel_half
        self.field_id = field_id
        self.truth = truth

    def observe(self) -> Perception:
        """注入一个**平移**的误差（区间宽度不变）—— 见模块注释的机制说明。"""
        return observe(self.truth, self.eps, rel_half=self.rel_half,
                       field_id=self.field_id)


class EmpiricalErrorSource:
    """**实测误差源**（接口就绪，待真实 `(检测密度, 真值密度)` 配对接入）。

    与合成源的区别：误差**不再是一个统一的 ε**，而是
      · **乘性偏置** `bias`（NMS 少计 → 检测值系统性低于真值）
      · **随机散度** `spread`（逐田块波动）

    【怎么从真实数据估这两个参数】
      · `bias   = median(log(检测密度) - log(真值密度))`
      · `spread = quantile(|log(检测) - log(真值) - bias|, 0.9)`
    （样方真值 → `Kwon&Kim精读` 的校准集对应物）
    """

    name = "empirical"

    def __init__(self, *, bias: float = 0.0, spread: float = 0.0,
                 rel_half: float, field_id: str, truth: float,
                 detected: float | None = None) -> None:
        self.bias = bias
        self.spread = spread
        self.rel_half = rel_half
        self.field_id = field_id
        self.truth = truth
        self.detected = detected

    def observe(self) -> Perception:
        """按实测参数生成一次观测。

        ★ 默认值让"队列里还没填 bias/spread"时也能跑（退化为**无偏置、纯固有误差**）——
          这样换源时**不需要先改队列**，可以先看曲线形状，再决定怎么填参数。
        """
        import math
        if self.detected is not None:
            center = float(self.detected)
        else:
            # ★ log 空间：乘性偏置在这里是**加法**，所以用 exp(bias) 还原
            center = float(self.truth) * math.exp(float(self.bias))
        # 实测散度并入区间宽度（偏置管"中心偏哪"，散度管"有多不确定"）
        half_ratio = math.hypot(float(self.rel_half), float(self.spread))
        half = abs(center) * half_ratio
        return Perception(
            field_id=self.field_id, count=center,
            lo=center - half, hi=center + half,
            eps=self.bias,
        )


#: 已实现的误差源（守卫断言"工厂与清单一致"，防新增时漏登记）
ERROR_SOURCES = {"synthetic": SyntheticErrorSource,
                 "empirical": EmpiricalErrorSource}

#: 各误差源**接受的**参数名。工厂只转发目标源认识的参数 ——
#: ★ 这样调用处可以**无条件传全套参数**，换源时一个字都不用改。
_SOURCE_PARAMS = {
    "synthetic": ("eps", "rel_half", "field_id", "truth"),
    "empirical": ("bias", "spread", "rel_half", "field_id", "truth", "detected"),
}


def make_source(kind: str, **kw):
    """误差源工厂 —— 让调用方**只改一个字符串**就能换源。

    ★ **只转发目标源认识的参数**（见 `_SOURCE_PARAMS`）。
      于是调用处可以无条件传 `eps / bias / spread / …` 全套，
      换成 `empirical` 时**不需要动调用处一个字符**。

      （第一版没做这件事，结果 `empirical` 收到 `eps` 直接 TypeError ——
       守卫真跑一次才暴露。**"可插拔"必须真的能插拔，不能只是形状像。**）
    """
    cls = ERROR_SOURCES.get(kind)
    if cls is None:
        raise ValueError(f"未知误差源 {kind!r}；可选：{sorted(ERROR_SOURCES)}")
    allowed = _SOURCE_PARAMS.get(kind, ())
    return cls(**{k: v for k, v in kw.items() if k in allowed})


# ---------------------------------------------------------------------------
# 参考决策
# ---------------------------------------------------------------------------


def decide(p: Perception, *, threshold: float = THRESHOLD,
           max_rel_width: float = MAX_REL_WIDTH) -> str:
    """四档决策 —— **委派给 `core.uncertainty`（单一真相）**。

    ★ 为什么不在这里自己写一份判据：U2 的生产门控用的是**同一条规则**。
      两处各写一份，迟早对不上（本项目已经总结过「两份地图必然对不上」）。
      所以**规则归 `core/uncertainty.py`**，本函数只做**标签适配**：

          生产档位                        → 评测标签
          ADVISE / ADVISE_WITH_INTERVAL   →  ABOVE / BELOW（按 side）
          QUALITATIVE                     →  BANDED
          ESCALATE                        →  ABSTAIN

      评测关心的是"**决策有没有变**"，所以把"正常建议"和"带区间建议"
      合成"实质决策"（ABOVE/BELOW）是**有意的**：它们都不是弃答。
    """
    d = uncertainty.evaluate(
        lo=p.lo, hi=p.hi, value=p.count,
        threshold=threshold, max_rel_width=max_rel_width)
    if d.level is uncertainty.Level.ESCALATE:
        return ABSTAIN
    if d.level is uncertainty.Level.QUALITATIVE:
        return BANDED
    return ABOVE if d.side is uncertainty.Side.ABOVE else BELOW


# ---------------------------------------------------------------------------
# 评测集（合成队列）
# ---------------------------------------------------------------------------

#: 12 块田。`rel_half` 表示"这块田的影像质量" —— 窄 = 影像好/覆盖全，宽 = 拍得少/帧差。
#: ★ 这是**待 U4 替换**的合成数据；它不需要真实，因为本评测测的是**规则灵敏度**。
#: 14 块田。`rel_half` 表示"这块田的影像质量" —— 窄 = 影像好/覆盖全，宽 = 拍得少/帧差。
#:
#: ★ **队列刻意围绕阈值对称**（`82/68`、`88/62`、`96/54`、`78/72` 各差 ±7/±13/±21/±3）。
#:   为什么：若靠近阈值的田块集中在阈值某一侧，误差曲线会**不对称**——
#:   而那个不对称来自**队列偏斜**，不是规则的属性。**那是伪结论，必须避免。**
#:   真实部署里当然可能不对称（取决于棉田密度的真实分布），但那是另一个问题。
#:
#: ★ 这是**待 U4 替换**的合成队列；它不需要真实，因为本评测测的是**规则灵敏度**。
COHORT: tuple[dict, ...] = (
    # ── 窄区间（影像质量好）· 成对对称地分布在阈值两侧 ──
    {"field": "A01", "truth": 96.0, "rel_half": 0.05},   # 距阈值 +21
    {"field": "A02", "truth": 88.0, "rel_half": 0.05},   # +13
    {"field": "A03", "truth": 82.0, "rel_half": 0.05},   # +7
    {"field": "A04", "truth": 78.0, "rel_half": 0.05},   # +3 → 跨阈值（区间窄）→ 降级表述
    {"field": "A05", "truth": 72.0, "rel_half": 0.05},   # -3 → 跨阈值（区间窄）→ 降级表述
    {"field": "A06", "truth": 68.0, "rel_half": 0.05},   # -7
    {"field": "A07", "truth": 62.0, "rel_half": 0.05},   # -13
    {"field": "A08", "truth": 54.0, "rel_half": 0.06},   # -21
    # ── 宽区间但「明确高于 / 低于阈值」→ 应给实质决策（宽 ≠ 不能答）──
    {"field": "B01", "truth": 132.0, "rel_half": 0.32},
    {"field": "B02", "truth": 34.0, "rel_half": 0.30},
    {"field": "B03", "truth": 118.0, "rel_half": 0.34},
    # ── 宽区间且跨阈值 → 应弃答（信息不足，建议补测）──
    {"field": "B04", "truth": 73.0, "rel_half": 0.33},
    {"field": "B05", "truth": 77.0, "rel_half": 0.31},
    {"field": "B06", "truth": 79.0, "rel_half": 0.30},
)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


@dataclass
class TierMetrics:
    """某个 ε 上的一组指标。"""

    eps: float
    n: int = 0
    coverage_rate: float = 0.0      #: 区间仍覆盖真值的比例（校准是否失效）
    misjudge_rate: float = 0.0      #: 与无误差基准不一致的比例（**只看实质决策之间**）
    abstain_recall: float | None = None     #: 该弃答时弃答的比例
    abstain_specificity: float | None = None  #: 不该弃答时没弃的比例
    answer_coverage: float = 0.0    #: 作出实质决策的比例
    counts: dict[str, int] = field(default_factory=dict)
    details: list[dict] = field(default_factory=list)


def evaluate_tier(eps: float, *, cohort=COHORT, threshold: float = THRESHOLD,
                  max_rel_width: float = MAX_REL_WIDTH,
                  source_kind: str = "synthetic") -> TierMetrics:
    """在某个误差水平上跑一遍队列，算一组指标。

    基准（`reference`）是**同一批田块在 ε=0 时的决策** ——
    因为本评测问的是"**误差让决策偏了多少**"，不是"决策本身对不对"。

    Args:
        source_kind: **误差源类型**（`synthetic` / `empirical`）。
            ★ 换成 `empirical` 时，`eps` 参数被忽略（误差由实测参数决定），
            指标、曲线、门禁**一行都不用改** —— 这正是抽象出误差源的目的。
    """
    # 基准恒用合成源的 ε=0（"无误差"这个参照系本身不该随误差源变化）
    refs: dict[str, str] = {}
    for row in cohort:
        p0 = observe(row["truth"], 0.0, rel_half=row["rel_half"],
                     field_id=row["field"])
        refs[row["field"]] = decide(p0, threshold=threshold,
                                    max_rel_width=max_rel_width)

    m = TierMetrics(eps=eps, n=len(cohort))
    covered = 0
    answered = 0
    # 误判：基准是实质决策、当前也是实质决策，但两者不同
    both_answer = 0
    mismatch = 0
    # 弃答双向
    should_abstain = 0
    abstained = 0            # 该弃且弃了
    should_answer = 0
    did_not_abstain = 0      # 不该弃且没弃

    for row in cohort:
        fid = row["field"]
        truth = float(row["truth"])
        # ★ 唯一的改动：观测由**误差源对象**产生，而不是直接调 observe()
        p = make_source(source_kind, eps=eps, rel_half=row["rel_half"],
                        field_id=fid, truth=truth,
                        **{k: v for k, v in row.items()
                           if k in ("bias", "spread", "detected")}).observe()
        got = decide(p, threshold=threshold, max_rel_width=max_rel_width)
        ref = refs[fid]

        m.counts[got] = m.counts.get(got, 0) + 1
        if covers(p, truth):
            covered += 1
        if got in ANSWERING:
            answered += 1

        if ref in ANSWERING:
            should_answer += 1
            if got in ANSWERING:
                did_not_abstain += 1
        else:
            should_abstain += 1
            if got == ABSTAIN:
                abstained += 1

        if ref in ANSWERING and got in ANSWERING:
            both_answer += 1
            if got != ref:
                mismatch += 1

        m.details.append({
            "field": fid, "truth": truth, "point": round(p.count, 2),
            "interval": [round(p.lo, 2), round(p.hi, 2)],
            "ref": ref, "got": got, "covered": covers(p, truth),
        })

    n = max(len(cohort), 1)
    m.coverage_rate = covered / n
    m.answer_coverage = answered / n
    m.misjudge_rate = (mismatch / both_answer) if both_answer else 0.0
    m.abstain_recall = (abstained / should_abstain) if should_abstain else None
    m.abstain_specificity = ((did_not_abstain / should_answer)
                             if should_answer else None)
    return m


def run_sweep(*, eps_grid=EPS_GRID, **kw) -> list[TierMetrics]:
    return [evaluate_tier(e, **kw) for e in eps_grid]


def reliability_threshold(tiers: list[TierMetrics], *,
                          limit: float = 0.10) -> float | None:
    """**可靠性阈值**：误判率**首次**超过 `limit` 的那个 ε。

    ★ 取"首次超过"而不是"最大还行的 ε" —— 后者会让报告在非单调数据上跳来跳去。
    ★ 若从头到尾都没超过，返回 `None`（表示**在这个网格内没找到边界**，不是"无限可靠"）。
    """
    for t in tiers:
        if t.misjudge_rate > limit:
            return t.eps
    return None


def coverage_breakdown(tiers: list[TierMetrics], *,
                       limit: float = 0.10) -> float | None:
    """**覆盖率跌破名义值超过 `limit`** 的那个 ε（校准失效点）。"""
    target = NOMINAL_COVERAGE - limit
    for t in tiers:
        if t.coverage_rate < target:
            return t.eps
    return None


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _ascii_curve(values: list[float], *, width: int = 20) -> str:
    """把 0–1 的序列画成一行 ASCII 条（终端里能直接看形状）。"""
    out = []
    for v in values:
        n = int(round(max(0.0, min(1.0, v)) * width))
        out.append("█" * n + "·" * (width - n))
    return " | ".join(out)


def render_report(tiers: list[TierMetrics], *, limit: float = 0.10) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("第六类评测集 · 感知误差注入（零 LLM / 零外部数据）")
    lines.append("=" * 78)
    lines.append(f"队列: {len(COHORT)} 块田 ｜ 阈值 {THRESHOLD} 个/㎡ ｜ "
                 f"名义覆盖率 {NOMINAL_COVERAGE:.0%} ｜ 弃答宽度上限 {MAX_REL_WIDTH:.0%}")
    lines.append("")
    lines.append(f"{'ε':>5} {'覆盖面':>7} {'误判率':>7} {'作答率':>7} "
                 f"{'弃答召回':>9} {'弃答特异':>9}   档位分布")
    lines.append("-" * 78)
    for t in tiers:
        rec = "—" if t.abstain_recall is None else f"{t.abstain_recall:.0%}"
        spec = "—" if t.abstain_specificity is None else f"{t.abstain_specificity:.0%}"
        dist = " ".join(f"{k}:{v}" for k, v in sorted(t.counts.items()))
        lines.append(f"{t.eps:>5.0%} {t.coverage_rate:>7.0%} {t.misjudge_rate:>7.0%} "
                     f"{t.answer_coverage:>7.0%} {rec:>9} {spec:>9}   {dist}")
    lines.append("")
    lines.append("退化曲线（每格 = 一个 ε，长度 ∝ 比例）")
    lines.append("  覆盖率: " + _ascii_curve([t.coverage_rate for t in tiers]))
    lines.append("  误判率: " + _ascii_curve([t.misjudge_rate for t in tiers]))
    lines.append("  弃答率: " + _ascii_curve([1.0 - t.answer_coverage for t in tiers]))
    lines.append("")

    rel = reliability_threshold(tiers, limit=limit)
    cal = coverage_breakdown(tiers, limit=limit)
    lines.append(f"★ 可靠性阈值（误判率首次 > {limit:.0%}）: "
                 + (f"ε = {rel:.0%}" if rel is not None else "本网格内未出现"))
    lines.append(f"★ 校准失效点（覆盖率跌破 {NOMINAL_COVERAGE - limit:.0%}）: "
                 + (f"ε = {cal:.0%}" if cal is not None else "本网格内未出现"))
    lines.append("")
    lines.append(f"  注：{len(COHORT)} 块田 → 覆盖率与误判率的**粒度**分别约 "
                 f"{1/len(COHORT):.0%}；小差异在采样噪声内，不要过度解读。")
    lines.append("怎么读：ε 是「检测值偏了多少」的相对比例。ε=0 是校准良好。")
    lines.append("        ★ 误差是**平移**不是变宽 —— 所以系统会**自信地答错**，")
    lines.append("          这正是域偏移下真实的风险形态。")
    lines.append("        ★ 队列围绕阈值对称，所以曲线应呈 V 形；若不对称，先查队列偏斜。")
    lines.append("=" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 门禁（第 43 项：**真的返回失败**）
# ---------------------------------------------------------------------------


def check_gate(tiers: list[TierMetrics], *, limit: float = 0.10) -> list[str]:
    """返回**违规说明**列表。空列表 = 通过。

    四条门禁，每条都对应一个**会让结论失效**的具体失效模式：

    ① **ε=0 必须校准良好** —— 否则整条基准就是错的，后面所有数都没意义；
    ② **ε=0 误判率必须为 0** —— 基准与自己比当然应为 0，不为 0 说明基准计算有 bug；
    ③ **必须存在可用区**（某个 ε ≤ 10% 上误判 ≤ limit）—— 否则这套决策在真实误差下
       根本没有可用空间，"能工作"就是谎话；
    ④ **大误差必须显著退化**（阳性对照）—— 若 ε=40% 时误判率仍 ≈ 0，
       说明**注入没生效**，这条曲线是平的假数据。
    """
    violations: list[str] = []
    if not tiers:
        return ["没有任何评测结果"]

    base = tiers[0]
    if base.eps > 1e-9:
        violations.append(f"① 首个 ε 不是 0（实际 {base.eps}），无法作基准")
    if base.coverage_rate < NOMINAL_COVERAGE - limit:
        violations.append(
            f"① ε=0 时覆盖率只有 {base.coverage_rate:.0%}，"
            f"低于名义值 {NOMINAL_COVERAGE:.0%}（基准不可信）")
    if base.misjudge_rate > 1e-9:
        violations.append(
            f"② ε=0 时误判率应为 0，实际 {base.misjudge_rate:.1%}（基准计算有 bug）")

    # ⚠️ **必须排除 ε=0**：基线恒可用，把它算进可用区会让这条门禁永远通过
    #    （守卫实测抓到的洞）。要问的是**真的有一点误差之后**还能用吗。
    usable = [t for t in tiers
              if 1e-9 < t.eps <= 0.10 + 1e-9 and t.misjudge_rate <= limit]
    if not usable:
        violations.append(
            f"③ 在 ε ≤ 10% 内没有任何一档误判率 ≤ {limit:.0%}（决策无可用区）")

    big = [t for t in tiers if t.eps >= 0.30 - 1e-9]
    if big and max(t.misjudge_rate for t in big) <= base.misjudge_rate:
        violations.append(
            "④ 阳性对照失败：ε ≥ 30% 时误判率没有超过基准 —— **注入可能没生效**")

    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="感知误差注入评测（第六类）")
    ap.add_argument("--limit-misjudge", type=float, default=0.10,
                    help="误判率门限（默认 10%%）")
    ap.add_argument("--json-out", default="",
                    help="把逐田详情写成 JSON（便于画图/复核）")
    ap.add_argument("--quiet", action="store_true", help="只输出门禁结论")
    args = ap.parse_args()

    tiers = run_sweep()
    if not args.quiet:
        print(render_report(tiers, limit=args.limit_misjudge))

    violations = check_gate(tiers, limit=args.limit_misjudge)
    rel = reliability_threshold(tiers, limit=args.limit_misjudge)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "threshold": THRESHOLD,
            "nominal_coverage": NOMINAL_COVERAGE,
            "max_rel_width": MAX_REL_WIDTH,
            "reliability_threshold": rel,
            "tiers": [
                {"eps": t.eps, "coverage_rate": t.coverage_rate,
                 "misjudge_rate": t.misjudge_rate,
                 "answer_coverage": t.answer_coverage,
                 "abstain_recall": t.abstain_recall,
                 "abstain_specificity": t.abstain_specificity,
                 "counts": t.counts, "details": t.details}
                for t in tiers
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.quiet:
            print(f"[落盘] 逐田详情 → {args.json_out}")

    if violations:
        print()
        print("✗ 门禁未通过：")
        for v in violations:
            print("   " + v)
        return 1

    print(f"✓ 门禁通过（{len(tiers)} 档全部达标）"
          + (f"；可靠性阈值 ε = {rel:.0%}" if rel is not None else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())