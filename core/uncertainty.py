# -*- coding: utf-8 -*-
"""不确定性门控 —— **这个数值能不能拿去做决策**。

【它是第几道闸门】本项目有三道**正交**闸门，各管一件事：

    `core/query_router.py`   →  「**要不要检索**」
    `core/tools/capabilities.py` →  「**准不准调用这个工具**」
    **本模块**                →  「**这个数值能不能拿去做决策**」

    三者不重叠：前两道管"取不取"，这一道管"取了之后敢不敢用"。

【为什么需要它】
    感知层给出的不是真值，是**带区间的估计**。同一个"76 个/㎡"，
    在区间 `[74, 78]` 下是"确定可采收"，在 `[50, 102]` 下是"根本不知道"。
    **如果不管区间宽窄一律照答，系统会在最不该自信的地方最自信。**

【四档动作】（`docs/ROADMAP.md` U2 的第 16 项）

    | 档位 | 触发条件 | 允许做的事 |
    |---|---|---|
    | `ADVISE` | 区间不跨阈值 + 区间窄 | 正常给建议，可以带数字 |
    | `ADVISE_WITH_INTERVAL` | 区间不跨阈值 + 区间**宽** | 可以给建议，但**必须同时给出区间** |
    | `QUALITATIVE` | 区间**跨**阈值 + 区间窄 | **只能说"接近阈值"这类定性话**，不许下确定结论 |
    | `ESCALATE` | 区间**跨**阈值 + 区间**宽**（或无区间） | **弃答**：说明信息不足、建议补飞或人工踏查 |

【★★ "LLM 不可越权硬门"是怎么实现的（这是本模块的核心）】

    分**两半**，缺一不可：

    ① **前置**：`constraint_text()` 把"这一档允许/禁止什么"写成**硬约束**
       注入提示词。★ 但**提示词不是保证** —— 所以还需要 ↓
    ② **后置**：`audit()` 对模型输出做**确定性检查**，看它有没有超出允许范围。

    ★ 这一半是必须的。本项目反复强调"**守卫抛异常时若放行，等于没有守卫**"；
      同理，**只在提示词里说"不许下结论"，等于没有门控** —— 模型可以不听。

【★ `audit()` 的已知局限（诚实声明）】

    它是**词面粗筛**，不是语义理解。所以：
    · **可能误报**（一句"如果检测准确则可采收"条件句会被抓）；
    · 因此默认只**记录与告警**，不自动改写回答（改写比误报更危险）；
    · 调用方可以选 `strict=True` 让它把违规**当作失败向上报**。

    ★ 这与 `01` 号 §5.5 记的 `is_refusal` 教训同源：
      **词面判定一定会误判，所以要限制它做什么，而不是假装它没有误差。**
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    """区间相对阈值的位置（**与"能不能用"是两件事**，别混）。"""

    ABOVE = "ABOVE"        #: 区间整体高于阈值
    BELOW = "BELOW"        #: 区间整体低于阈值
    STRADDLE = "STRADDLE"  #: 区间跨越阈值 —— **判据只看这个，不看点估计**
    UNKNOWN = "UNKNOWN"    #: 没拿到区间


class Level(str, Enum):
    """四档动作。**由区间决定，不由模型决定。**"""

    ADVISE = "ADVISE"                            #: 正常建议
    ADVISE_WITH_INTERVAL = "ADVISE_WITH_INTERVAL"  #: 带区间建议
    QUALITATIVE = "QUALITATIVE"                  #: 降级为定性表述
    ESCALATE = "ESCALATE"                        #: 弃答 / 建议补测


#: 各档位**允许**的动作（写进提示词，也用于审计）
ALLOWED_ACTIONS: dict[Level, frozenset[str]] = {
    Level.ADVISE: frozenset({
        "给出明确建议", "引用具体数值", "说明依据",
    }),
    Level.ADVISE_WITH_INTERVAL: frozenset({
        "给出明确建议", "引用具体数值", "**必须同时给出区间上下界**", "说明依据",
    }),
    Level.QUALITATIVE: frozenset({
        "给出定性表述（如「接近阈值」「大致相当」）",
        "说明为什么无法给确定结论", "建议人工确认",
    }),
    Level.ESCALATE: frozenset({
        "说明信息不足", "说明缺什么信息", "建议补飞 / 人工踏查",
    }),
}

#: 各档位**禁止**的动作（★ 后置审计就查这些）
FORBIDDEN_ACTIONS: dict[Level, frozenset[str]] = {
    Level.ADVISE: frozenset(),
    Level.ADVISE_WITH_INTERVAL: frozenset({"只给点估计而不给区间"}),
    Level.QUALITATIVE: frozenset({"给出确定结论", "声称某动作一定可行"}),
    Level.ESCALATE: frozenset({"给出任何采收建议", "声称数据可靠", "给出确定结论"}),
}


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

#: 默认"区间太宽，宽到没法用"的相对宽度上限。
#: 与 `tests/eval_error_injection.py` 用的是同一个概念值（那边是评测档位，这边是生产门控）。
DEFAULT_MAX_REL_WIDTH = 0.30


@dataclass(frozen=True)
class GateDecision:
    """门控结论。**不可变** —— 一旦判定，下游只能照着做。"""

    level: Level
    side: Side
    reason: str
    threshold: float
    lo: float | None = None
    hi: float | None = None
    rel_width: float | None = None
    #: 允许 / 禁止的动作集（供提示词与审计共用）
    allowed: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset()

    @property
    def must_abstain(self) -> bool:
        return self.level is Level.ESCALATE

    @property
    def must_quote_interval(self) -> bool:
        return self.level is Level.ADVISE_WITH_INTERVAL

    def __str__(self) -> str:                                 # noqa: D105
        rng = (f"[{self.lo:.1f}, {self.hi:.1f}]"
               if self.lo is not None and self.hi is not None else "无区间")
        return (f"{self.level.value}（{self.side.value} · {rng} vs 阈值 "
                f"{self.threshold:g}）：{self.reason}")


def evaluate(
    *,
    lo: float | None,
    hi: float | None,
    threshold: float,
    value: float | None = None,
    max_rel_width: float = DEFAULT_MAX_REL_WIDTH,
) -> GateDecision:
    """**唯一判据**：拿到一个带区间的数值，决定能用它做什么。

    ★ **Fail-Closed**：区间缺失 / 反向 / 阈值非法 → 一律 `ESCALATE`。
      "无法判断"必须落到**最保守**的那一档，而不是放行。

    ★ 点估计 `value` **只用于算相对宽度**（分母），**不参与"高于/低于"的判定** ——
      "高于低于"看的是**区间**，不是点。这是本模块与"拍脑袋阈值"的根本区别。
    """
    if not (isinstance(threshold, (int, float)) and threshold > 0):
        return _escalate(f"阈值非法（{threshold!r}），无法判断", threshold)

    if lo is None or hi is None:
        return _escalate("没有拿到区间 —— 无从判断这个数可用到什么程度", threshold)

    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return _escalate(f"区间不是数字（lo={lo!r}, hi={hi!r}）", threshold)

    if lo_f > hi_f:
        return _escalate(f"区间反向（lo={lo_f} > hi={hi_f}），数据有误", threshold)
    if hi_f <= 0:
        return _escalate(f"区间上限 {hi_f} 非正，无法计算相对宽度", threshold)

    center = float(value) if value is not None else (lo_f + hi_f) / 2.0
    rel_width = (hi_f - lo_f) / max(abs(center), 1e-9)
    narrow = rel_width <= max_rel_width

    # ① 区间整体高于阈值
    if lo_f > threshold:
        if narrow:
            return GateDecision(
                Level.ADVISE, Side.ABOVE,
                f"区间整体高于阈值（下界 {lo_f:.1f} > {threshold:g}），"
                f"相对宽度 {rel_width:.0%} 在 {max_rel_width:.0%} 以内",
                threshold, lo_f, hi_f, rel_width,
                ALLOWED_ACTIONS[Level.ADVISE], FORBIDDEN_ACTIONS[Level.ADVISE])
        return GateDecision(
            Level.ADVISE_WITH_INTERVAL, Side.ABOVE,
            f"区间整体高于阈值，但**偏宽**（相对宽度 {rel_width:.0%} > "
            f"{max_rel_width:.0%}）—— 给建议时必须带上区间",
            threshold, lo_f, hi_f, rel_width,
            ALLOWED_ACTIONS[Level.ADVISE_WITH_INTERVAL],
            FORBIDDEN_ACTIONS[Level.ADVISE_WITH_INTERVAL])

    # ② 区间整体低于阈值
    if hi_f < threshold:
        if narrow:
            return GateDecision(
                Level.ADVISE, Side.BELOW,
                f"区间整体低于阈值（上界 {hi_f:.1f} < {threshold:g}），"
                f"相对宽度 {rel_width:.0%} 在 {max_rel_width:.0%} 以内",
                threshold, lo_f, hi_f, rel_width,
                ALLOWED_ACTIONS[Level.ADVISE], FORBIDDEN_ACTIONS[Level.ADVISE])
        return GateDecision(
            Level.ADVISE_WITH_INTERVAL, Side.BELOW,
            f"区间整体低于阈值，但**偏宽**（相对宽度 {rel_width:.0%}）—— "
            f"给结论时必须带上区间",
            threshold, lo_f, hi_f, rel_width,
            ALLOWED_ACTIONS[Level.ADVISE_WITH_INTERVAL],
            FORBIDDEN_ACTIONS[Level.ADVISE_WITH_INTERVAL])

    # ③ 跨阈值：区间够窄 → 只能说定性话；太宽 → 弃答
    if narrow:
        return GateDecision(
            Level.QUALITATIVE, Side.STRADDLE,
            f"区间**跨越**阈值（{lo_f:.1f}—{hi_f:.1f} 含 {threshold:g}），"
            f"但宽度可控（{rel_width:.0%}）—— 只能给定性判断",
            threshold, lo_f, hi_f, rel_width,
            ALLOWED_ACTIONS[Level.QUALITATIVE], FORBIDDEN_ACTIONS[Level.QUALITATIVE])
    return GateDecision(
        Level.ESCALATE, Side.STRADDLE,
        f"区间**跨越**阈值且过宽（相对宽度 {rel_width:.0%} > "
        f"{max_rel_width:.0%}）—— 信息不足，应补测",
        threshold, lo_f, hi_f, rel_width,
        ALLOWED_ACTIONS[Level.ESCALATE], FORBIDDEN_ACTIONS[Level.ESCALATE])


def _escalate(reason: str, threshold: float) -> GateDecision:
    """最保守的出口 —— 所有"无法判断"都走这里。"""
    return GateDecision(
        Level.ESCALATE, Side.UNKNOWN, reason, threshold,
        None, None, None,
        ALLOWED_ACTIONS[Level.ESCALATE], FORBIDDEN_ACTIONS[Level.ESCALATE])


# ---------------------------------------------------------------------------
# ① 前置：硬约束文本（注入提示词）
# ---------------------------------------------------------------------------


def constraint_text(d: GateDecision) -> str:
    """生成要注入提示词的**硬约束**。

    ★ 措辞刻意强硬（"**必须**"/"**不得**"），因为它要与模型自己的倾向对抗。
    ★ 但**不要指望它就够了** —— 后置审计是必须的另一半。
    """
    lines = [
        "【不确定性门控 · 硬约束】",
        f"本次查询的数值可用性已被判定为：**{d.level.value}**",
        f"判据：{d.reason}",
        "",
        "你**允许**做的事：",
    ]
    lines += [f"  · {a}" for a in sorted(d.allowed)]
    if d.forbidden:
        lines += ["", "你**不得**做的事："]
        lines += [f"  · {a}" for a in sorted(d.forbidden)]
    if d.level is Level.ESCALATE:
        lines += ["", "本次必须**弃答**：说明信息不足、缺什么，并建议补飞或人工踏查。"
                      "**不要**给出任何具体的采收/农事操作建议。"]
    elif d.level is Level.QUALITATIVE:
        lines += ["", "本次只能给**定性**判断（如「接近阈值」）。"
                      "**不要**下确定结论，也不要声称某项操作一定可行。"]
    elif d.level is Level.ADVISE_WITH_INTERVAL:
        lines += ["", f"本次给结论时**必须同时写出区间**："
                      f"[{d.lo:.1f}, {d.hi:.1f}]。只给单个数视为越权。"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ② 后置：越权审计（确定性检查）
# ---------------------------------------------------------------------------

#: 各档位下"越权"的**词面特征**。
#: ★ 这是**粗筛**，会有误报（见模块注释的已知局限）。宁可误报，不可漏报 ——
#: 因为漏报意味着系统在最该保守的时候给了确定结论。
_VERDICT_PATTERNS: dict[Level, tuple[str, ...]] = {
    Level.ADVISE: (),
    Level.ADVISE_WITH_INTERVAL: (),
    Level.QUALITATIVE: (
        "建议采收", "可以采收", "应当采收", "肯定", "一定会", "必然",
        "确定可以", "没有问题", "放心",
    ),
    Level.ESCALATE: (
        "建议采收", "可以采收", "应当采收", "赶紧采", "马上采",
        "亩产", "产量为", "密度为", "定为",           # 精确数值断言
        "确定可以", "没有问题", "放心", "肯定",
    ),
}


@dataclass(frozen=True)
class AuditResult:
    """越权审计结果。"""

    ok: bool
    level: Level
    hits: tuple[str, ...] = ()
    reason: str = ""

    def __bool__(self) -> bool:                               # noqa: D105
        return self.ok

    def __str__(self) -> str:                                 # noqa: D105
        if self.ok:
            return f"未发现越权（{self.level.value}）"
        return f"**疑似越权**（{self.level.value}）：命中 {list(self.hits)}"


def audit(text: str, d: GateDecision) -> AuditResult:
    """检查模型输出有没有超出这一档允许的范围。

    ★ **只是粗筛**。默认调用方应把它当**告警**用（记日志 / 进 trace），
      而不是直接改写回答 —— 误报时改写造成的伤害比违规更大。
    """
    if not isinstance(text, str) or not text.strip():
        # 空回答不是"越权"，是"没答" —— 由别的机制处理
        return AuditResult(True, d.level)
    hits = tuple(p for p in _VERDICT_PATTERNS.get(d.level, ()) if p in text)
    if hits:
        return AuditResult(False, d.level, hits,
                           f"该档位禁止「{', '.join(sorted(d.forbidden))}」，"
                           f"但输出命中 {list(hits)}")
    return AuditResult(True, d.level)


__all__ = [
    "ALLOWED_ACTIONS", "DEFAULT_MAX_REL_WIDTH", "FORBIDDEN_ACTIONS",
    "AuditResult", "GateDecision", "Level", "Side", "audit",
    "constraint_text", "evaluate",
]