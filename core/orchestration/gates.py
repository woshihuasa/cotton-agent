# -*- coding: utf-8 -*-
"""确定性门禁 —— **用代码而不是提示词**保证结果可信。

【为什么需要门禁】模型会在回答里写出"前后不一致"的数字，或者越界的建议。
        项目现在靠 `SYSTEM_PROMPT` 的五条【回答质量约束】去"请求"它别这样 ——
        **那是提示，不是保证**。门禁是**代码级的拒绝**。

【★ 核心纪律：Fail-Closed（失败即关闭）】
        每一个门禁函数都把自己的逻辑包在 `try/except` 里，
        **门禁自身出错时判"不通过"，而不是放行**。

        这与 `core/tools/capabilities.guard()` 同一条纪律 ——
        本项目已经在工具闸门上定过这条规矩：
        **守卫抛异常时若放行，等于没有守卫。**

        反过来（Fail-Open）的后果是：门禁写错了、依赖挂了、数值是 None，
        **系统会静默地全部通过** —— 这比没有门禁更糟，因为它给了虚假的安全感。

【三类门禁】（对应 `docs/ORCHESTRATION.md` §5.2 的 `gate.*`）

        · `numeric_consistency` —— 同一数字在多个来源里是否对得上（现有"数字闸"的引擎化）
        · `range`               —— 数值是否越界（现有 `check_data_ranges` 的经验）
        · `mutual_exclusion`    —— 两个互斥标识是否同时出现（现有 L4"要素闸"的引擎化）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class GateError(RuntimeError):
    """门禁自身的配置或调用错误（**注意：这不会导致放行，只会导致拒绝**）。"""


@dataclass(frozen=True)
class GateResult:
    """门禁结论。`passed=False` 时 `reason` 必须说清**为什么**。

    ★ 刻意**不做成** `bool` —— 布尔值会丢掉"为什么没通过"，
    而门禁的价值有一半在"能告诉人哪里不对"。
    """

    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:                              # noqa: D105
        return self.passed

    def __str__(self) -> str:                                # noqa: D105
        return ("通过" if self.passed else "拒绝") + f"：{self.reason}"


def _reject(reason: str, **details: Any) -> GateResult:
    return GateResult(False, reason, details)


def _pass(reason: str, **details: Any) -> GateResult:
    return GateResult(True, reason, details)


def _fail_closed(gate_name: str, exc: BaseException) -> GateResult:
    """门禁自身出错时的统一出口 —— **永远拒绝**。"""
    return GateResult(
        False,
        f"门禁 {gate_name} 自身异常，按 Fail-Closed 判不通过："
        f"{type(exc).__name__}: {exc}",
        {"error": f"{type(exc).__name__}: {exc}", "fail_closed": True},
    )


# ---------------------------------------------------------------------------
# 数值提取
# ---------------------------------------------------------------------------

#: 匹配数字（含千分位、小数、可选负号）。刻意**不匹配**纯年份（4 位且无小数点）以外的噪声。
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def extract_numbers(value: Any) -> list[float]:
    """从任意值里抽出所有数字。

    · `int/float` → 直接返回
    · `str` → 正则抽取（`"亩产 125.1 公斤"` → `[125.1]`）
    · `dict/list` → 递归
    · 其它 → 空列表

    ★ **不抛异常**：抽不出数字就返回空列表，由调用方决定"空"算不算失败。
    """
    if isinstance(value, bool):        # bool 是 int 的子类，必须先挡掉
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        out: list[float] = []
        for m in _NUM_RE.finditer(value):
            raw = m.group(0).replace(",", "")
            try:
                out.append(float(raw))
            except ValueError:
                continue
        return out
    if isinstance(value, dict):
        nums: list[float] = []
        for v in value.values():
            nums.extend(extract_numbers(v))
        return nums
    if isinstance(value, (list, tuple, set)):
        nums = []
        for v in value:
            nums.extend(extract_numbers(v))
        return nums
    return []


# ---------------------------------------------------------------------------
# 门禁一：数值一致性
# ---------------------------------------------------------------------------


def numeric_consistency(
    sources: list[Any],
    target: Any,
    *,
    tolerance: float = 0.01,
) -> GateResult:
    """同一数字在**多个来源**里是否对得上。

    `tolerance` 是**相对误差**（默认 1%）：`125.1` 与 `125` 视为一致。

    ★ **不通过的情形（三种，都要拒绝）**：
      ① 任一来源抽不出数字 → 无从比对 → **拒绝**（Fail-Closed）
      ② 目标抽不出数字 → **拒绝**
      ③ 数字两两之差超过容差 → **拒绝**

    "抽不出数字就放行"是最常见的 Fail-Open 写法，本函数**刻意不那样做**。
    """
    try:
        if not sources:
            return _reject("sources 为空，没有可比对的来源")

        target_nums = extract_numbers(target)
        if not target_nums:
            return _reject("target 里抽不出任何数字，无从比对",
                           target_repr=str(target)[:120])

        source_nums: list[tuple[int, float]] = []
        for i, src in enumerate(sources):
            nums = extract_numbers(src)
            if not nums:
                return _reject(f"来源 #{i} 里抽不出任何数字，无需比对即为不一致",
                               source_index=i, source_repr=str(src)[:120])
            for n in nums:
                source_nums.append((i, n))

        # 逐个目标数字，看是否能在来源里找到"对得上"的值
        bad: list[dict[str, Any]] = []
        for t in target_nums:
            matched = any(_rel_close(t, s, tolerance) for _, s in source_nums)
            if not matched:
                bad.append({"target": t,
                            "nearest": _nearest(t, [s for _, s in source_nums])})
        if bad:
            return _reject(
                f"{len(bad)} 个目标数字在来源里找不到一致值（容差 {tolerance:.1%}）",
                mismatches=bad[:8], tolerance=tolerance)

        return _pass(
            f"{len(target_nums)} 个目标数字均在来源中找到一致值",
            tolerance=tolerance, source_count=len(sources))
    except BaseException as e:                               # noqa: BLE001
        return _fail_closed("numeric_consistency", e)


def _rel_close(a: float, b: float, tol: float) -> bool:
    """相对误差比较。`a == b == 0` 视为一致。"""
    if a == b:
        return True
    denom = max(abs(a), abs(b))
    if denom == 0:
        return True
    return abs(a - b) / denom <= tol


def _nearest(x: float, pool: list[float]) -> float | None:
    if not pool:
        return None
    return min(pool, key=lambda v: abs(v - x))


# ---------------------------------------------------------------------------
# 门禁二：范围
# ---------------------------------------------------------------------------


def range_check(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> GateResult:
    """数值是否落在 `[minimum, maximum]` 内。

    ★ 以下情形**全部拒绝**（Fail-Closed）：
      · 抽不出数字（无从判断）
      · 抽到**多个**数字（无法确定该比哪个 —— 猜一个就是 Fail-Open）
      · 越界
      · `minimum > maximum`（配置写反了）
    """
    try:
        if minimum is not None and maximum is not None and minimum > maximum:
            return _reject(f"范围配置非法：minimum({minimum}) > maximum({maximum})")

        nums = extract_numbers(value)
        if not nums:
            return _reject("抽不出数字，无法判断是否越界", value_repr=str(value)[:120])
        if len(nums) > 1:
            return _reject(
                f"抽到 {len(nums)} 个数字 {nums[:6]}，无法确定该比哪一个 —— "
                f"请先收敛为一个值", numbers=nums[:8])

        n = nums[0]
        if minimum is not None and n < minimum:
            return _reject(f"{n} 低于下限 {minimum}", value=n, minimum=minimum)
        if maximum is not None and n > maximum:
            return _reject(f"{n} 高于上限 {maximum}", value=n, maximum=maximum)
        return _pass(f"{n} 在范围内", value=n, minimum=minimum, maximum=maximum)
    except BaseException as e:                               # noqa: BLE001
        return _fail_closed("range", e)


# ---------------------------------------------------------------------------
# 门禁三：要素互斥
# ---------------------------------------------------------------------------

#: 默认的互斥要素对 —— 取自项目真实的地区语义：**两个不同地区不能同时作为一个答案的归属**。
#: ★ 这不是通用规则，是本项目的领域规则；`mutual_exclusion()` 允许调用方自己传。
DEFAULT_EXCLUSIVE_PAIRS: tuple[tuple[str, str], ...] = (
    ("南疆", "北疆"),
    ("机采", "手采"),
    ("春播", "秋播"),
)


def mutual_exclusion(
    a: Any,
    b: Any,
    *,
    pairs: tuple[tuple[str, str], ...] | None = None,
) -> GateResult:
    """两个**互斥标识**不能同时出现。

    典型场景：一个答案同时说"按南疆标准"和"按北疆标准"，
    或者同时给"机采"和"手采"的作业参数 —— **互相矛盾**。

    ★ 字符串双向包含都算命中（`a` 含左词且 `b` 含右词，或反过来）。
    ★ 抽不出文本 / 两者任一为空 → **拒绝**（无法判断即不通过）。
      ⚠️ 注意这条与"没有命中"不同：**空输入不等于"没有矛盾"**。
      但为了让单节点场景可用，`mutual_exclusion` 把"**两者都为空**"判**通过**（确实没有矛盾），
      "**只有一个为空**"判**拒绝**（另一半无从比较）。
    """
    try:
        use_pairs = pairs if pairs is not None else DEFAULT_EXCLUSIVE_PAIRS
        sa = _as_text(a)
        sb = _as_text(b)

        if not sa and not sb:
            return _pass("两侧均为空，不存在互斥冲突")
        if not sa or not sb:
            return _reject(
                "有一侧为空，无法判断是否互斥",
                a=sa[:80], b=sb[:80])

        hits: list[tuple[str, str]] = []
        for left, right in use_pairs:
            if (left in sa and right in sb) or (right in sa and left in sb):
                hits.append((left, right))

        if hits:
            return _reject(
                f"检测到互斥要素同时出现：{[f'{l}/{r}' for l, r in hits]}",
                conflicts=hits[:8])
        return _pass("未检测到互斥要素同时出现", checked_pairs=len(use_pairs))
    except BaseException as e:                               # noqa: BLE001
        return _fail_closed("mutual_exclusion", e)


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple, set)):
        return " ".join(_as_text(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_as_text(x) for x in v.values())
    return str(v)


# ---------------------------------------------------------------------------
# 统一入口（engine 按节点类型查它）
# ---------------------------------------------------------------------------

#: `gate.*` 节点类型 → 实现函数。**engine 只认这张表，不写 if/else 分支链。**
GATES = {
    "gate.numeric_consistency": numeric_consistency,
    "gate.range": range_check,
    "gate.mutual_exclusion": mutual_exclusion,
}


def run_gate(gate_type: str, **kwargs: Any) -> GateResult:
    """按类型跑一个门禁。**未知类型 → 拒绝（不是放行）。**"""
    fn = GATES.get(gate_type)
    if fn is None:
        return _reject(f"未知门禁类型 {gate_type!r}；已知: {sorted(GATES)}")
    try:
        return fn(**kwargs)
    except TypeError as e:
        # 参数名对不上（比如 YAML 里写了 minimum 但函数要 min）
        return _reject(f"门禁 {gate_type} 参数不匹配：{e}")
    except BaseException as e:                               # noqa: BLE001
        return _fail_closed(gate_type, e)


__all__ = [
    "DEFAULT_EXCLUSIVE_PAIRS", "GATES", "GateError", "GateResult",
    "extract_numbers", "mutual_exclusion", "numeric_consistency",
    "range_check", "run_gate",
]