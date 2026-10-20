# -*- coding: utf-8 -*-
"""校准与区间 —— 密度 → 带覆盖率保证的区间。

【这一层做什么】
  1. 用「有真值的校准集」测出检测器的偏置与散度
  2. 按域分层，各自算分位数（Mondrian）
  3. 给新样本套区间
  4. 报告域内 / 跨域的覆盖率

【关键设计：log 空间】
  NMS 少计是**乘性**的（密度越高漏越多），所以残差取
      s = log(检测密度) - log(真值密度)
  而不是差值。在 log 空间里乘性变加性，标准 split conformal 直接套，
  且让区间在高密度田不过窄、低密度田不过宽。

【不自己推数学】
  分位数就是排序取位置，numpy 一行。共形预测的"有限样本修正"
  体现在取分位数的位置公式里（见 conformal_quantile）。

【可单独验证】
  python calibrate.py --selftest
  → 用合成数据验证：覆盖率是否达到名义值、log空间是否正确、分层是否生效
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np


# ---------- 共形分位数 ----------


def conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    """分裂共形的分位数（含有限样本修正）。

    标准做法：取第 ceil((n+1)(1-alpha))/n 分位数。
    这个 +1 修正保证在可交换性下覆盖率 ≥ 1-alpha（有限样本，不是渐近）。

    例：n=100, alpha=0.1 → ceil(101*0.9)/100 = ceil(90.9)/100 = 91/100
        即取第 91 个最小的值（而不是第 90 个）—— 这个保守偏移就是保证的来源。
    """
    s = np.sort(np.asarray(scores, dtype=float))
    n = len(s)
    if n == 0:
        raise ValueError("校准集为空")
    k = int(math.ceil((n + 1) * (1.0 - alpha)))
    k = min(max(k, 1), n)          # 夹在 [1, n]
    return float(s[k - 1])


# ---------- 数据结构 ----------


@dataclass
class CalibrationPoint:
    """一个校准点：同一位置的检测密度 vs 真值密度。

    source 记录它来自哪个域（2024/2025/2026），用于分层校准。
    """

    detected: float          # 检测密度（个/m²）
    truth: float             # 真值密度（个/m²）
    domain: str = "default"  # 域标识
    field_id: str = ""
    # 真值本身的不确定度（两人计数的标准差），可选
    truth_sd: float | None = None

    @property
    def log_ratio(self) -> float:
        """log 空间残差：log(检测) - log(真值)。

        = log(检测/真值)。正值表示检测高估，负值表示少计。
        检测器系统性少计 → 这个值通常为负。
        """
        eps = 1e-9
        d = max(self.detected, eps)
        t = max(self.truth, eps)
        return math.log(d) - math.log(t)


@dataclass
class CalibrationModel:
    """一个域的校准模型 —— 这就是契约里 calibration_id 指向的东西。"""

    calibration_id: str
    domain: str
    alpha: float
    n_cal: int
    # log 空间下：偏置（残差中位数）+ 区间半宽（残差绝对值分位数）
    log_bias: float
    log_halfwidth: float
    # 经验覆盖率（在校准集自身上测的，会有轻度乐观，仅作参考）
    empirical_coverage: float
    # 方法标识
    method: str = "split_conformal_log"
    # ★ 小样本警示：n < 10 时必须标注不可靠
    reliable: bool = True

    def interval(self, detected: float) -> tuple[float, float]:
        """给一个检测密度，返回 (lo, hi) 区间。

        步骤：
          1. 转 log 空间：s = log(检测)
          2. 减偏置修正：s' = s - log_bias
          3. 加减半宽：[s' - hw, s' + hw]
          4. 转回原空间：exp(...)
        """
        eps = 1e-9
        s = math.log(max(detected, eps))
        center = s - self.log_bias
        lo = math.exp(center - self.log_halfwidth)
        hi = math.exp(center + self.log_halfwidth)
        return (lo, hi)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["log_bias"] = round(self.log_bias, 6)
        d["log_halfwidth"] = round(self.log_halfwidth, 6)
        d["empirical_coverage"] = round(self.empirical_coverage, 4)
        d["bias_factor"] = round(math.exp(self.log_bias), 4)
        d["width_factor"] = round(math.exp(2 * self.log_halfwidth), 4)
        return d


# ---------- 校准 ----------


def calibrate(
    points: list[CalibrationPoint],
    domain: str,
    alpha: float = 0.10,
    calibration_id: str | None = None,
) -> CalibrationModel:
    """在一个域上校准。

    偏置 = 残差的中位数（稳健，不受离群点影响）
    半宽 = |残差 - 偏置| 的 (1-alpha) 分位数

    这样做的含义：
      · 偏置修正把系统性少计拉回来（点估计更准）
      · 半宽给出随机波动的范围（区间宽度）
    """
    if not points:
        raise ValueError(f"域 {domain} 没有校准点")

    ratios = np.array([p.log_ratio for p in points], dtype=float)
    bias = float(np.median(ratios))
    centered = np.abs(ratios - bias)
    halfwidth = conformal_quantile(centered, alpha)

    # 经验覆盖率（自测，会略乐观 —— 因为用的是同一批数据）
    covered = np.mean(centered <= halfwidth)

    n = len(points)
    return CalibrationModel(
        calibration_id=calibration_id or f"cal-{domain}-n{n}",
        domain=domain,
        alpha=alpha,
        n_cal=n,
        log_bias=bias,
        log_halfwidth=halfwidth,
        empirical_coverage=float(covered),
        reliable=(n >= 10),
    )


def calibrate_stratified(
    points: list[CalibrationPoint],
    alpha: float = 0.10,
    min_per_domain: int = 5,
) -> dict[str, CalibrationModel]:
    """按域分层校准（Mondrian）。

    每个域各自算一套分位数 —— 这保证**组条件覆盖**：
    在每个域内部，覆盖率都达到名义值（而不是整体平均达到）。

    域样本量不足 min_per_domain 时，回退到「全体一个模型」，
    并在模型里标注（避免小样本算出无意义的区间）。
    """
    by_domain: dict[str, list[CalibrationPoint]] = {}
    for p in points:
        by_domain.setdefault(p.domain, []).append(p)

    models: dict[str, CalibrationModel] = {}
    large = {d: ps for d, ps in by_domain.items() if len(ps) >= min_per_domain}

    if not large:
        # 全部域都太小 → 用全体
        return {"__pooled__": calibrate(points, "pooled", alpha, "cal-pooled")}

    for d, ps in by_domain.items():
        if len(ps) >= min_per_domain:
            models[d] = calibrate(ps, d, alpha)
        else:
            # 小域 → 借全体模型（并标注它其实来自 pooled）
            pooled = calibrate(points, "pooled", alpha, "cal-pooled")
            m = CalibrationModel(
                calibration_id=f"cal-{d}-borrowed",
                domain=d, alpha=alpha, n_cal=len(ps),
                log_bias=pooled.log_bias,
                log_halfwidth=pooled.log_halfwidth,
                empirical_coverage=float("nan"),
                method="borrowed_from_pooled",
                reliable=False,
            )
            models[d] = m

    return models


# ---------- 覆盖率评估 ----------


@dataclass
class CoverageReport:
    """一次覆盖率评估的结果 —— 论文里那张表的来源。"""

    cal_domain: str
    test_domain: str
    n_test: int
    nominal: float
    empirical: float
    drop_pp: float = 0.0          # 相对名义值的掉幅（百分点）
    mean_width: float = 0.0        # 平均区间宽度（相对值）
    # 命中的是哪些点（供诊断）
    per_point: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cal_domain": self.cal_domain,
            "test_domain": self.test_domain,
            "n_test": self.n_test,
            "nominal": self.nominal,
            "empirical": round(self.empirical, 4),
            "drop_pp": round(self.drop_pp, 2),
            "mean_width": round(self.mean_width, 4),
        }


def evaluate_coverage(
    model: CalibrationModel,
    test_points: list[CalibrationPoint],
    test_domain: str = "",
) -> CoverageReport:
    """用某个域的校准模型，在另一批点上测覆盖率。

    ★ 这是论文的核心实验：
      · cal_domain == test_domain → 域内覆盖率（应 ≈ 名义值）
      · cal_domain != test_domain → 跨域覆盖率（会掉，掉多少就是发现）
    """
    if not test_points:
        return CoverageReport(model.domain, test_domain or model.domain,
                              0, 1 - model.alpha, float("nan"))

    hits = 0
    widths = []
    per_point = []

    for p in test_points:
        lo, hi = model.interval(p.detected)
        covered = (lo <= p.truth <= hi)
        hits += int(covered)
        widths.append((hi - lo) / max(p.detected, 1e-9))   # 相对宽度
        per_point.append({
            "field_id": p.field_id,
            "detected": round(p.detected, 3),
            "truth": round(p.truth, 3),
            "lo": round(lo, 3), "hi": round(hi, 3),
            "covered": covered,
        })

    emp = hits / len(test_points)
    nominal = 1 - model.alpha
    return CoverageReport(
        cal_domain=model.domain,
        test_domain=test_domain or model.domain,
        n_test=len(test_points),
        nominal=nominal,
        empirical=emp,
        drop_pp=(nominal - emp) * 100,
        mean_width=float(np.mean(widths)),
        per_point=per_point,
    )


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 置信区间 —— 小样本下比正态近似可靠。

    ★ 为什么必须报这个：n=36 时单个点即 2.8pp，
      不报区间就会被误读成精确数字。项目已有先例（n=17 时 1 条 = 5.9pp）。
    """
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ---------- 自检 ----------


def selftest() -> int:
    print("=" * 70)
    print("校准与区间 · 自检")
    print("=" * 70)
    ok = True
    rng = np.random.default_rng(42)

    # --- 1. 有限样本修正 ---
    print("\n[1] 共形分位数（有限样本修正）")
    scores = np.arange(1, 101, dtype=float)      # 1..100
    q = conformal_quantile(scores, 0.10)
    print(f"    100 个分数 1..100，alpha=0.1 → 分位数 = {q}")
    # ceil(101*0.9)=91 → 第91个 = 91
    good = q == 91.0
    print(f"    {'✓' if good else '✗'} 应为 91（不是 90 —— +1 修正是保证的来源）")
    ok = ok and good

    # --- 2. log 空间 ---
    print("\n[2] log 空间残差")
    p = CalibrationPoint(detected=700, truth=1000, domain="d1")
    lr = p.log_ratio
    print(f"    检测 700 / 真值 1000 → log_ratio = {lr:.4f}")
    print(f"    含义：检测只有真值的 {math.exp(lr)*100:.1f}%（系统性少计）")
    good = lr < 0
    print(f"    {'✓' if good else '✗'} 少计应为负值")
    ok = ok and good

    # --- 3. 合成数据校准：覆盖率应达标 ---
    print("\n[3] 合成校准（验证覆盖率）")
    n_cal = 200
    true_density = rng.uniform(300, 900, n_cal)
    # 模拟乘性少计：检测 = 真值 * 0.7 * lognormal噪声
    detected = true_density * 0.7 * rng.lognormal(0, 0.15, n_cal)
    cal_pts = [CalibrationPoint(detected=float(d), truth=float(t), domain="cal")
               for d, t in zip(detected, true_density)]
    model = calibrate(cal_pts, "cal", alpha=0.10)
    print(f"    偏置因子 = {math.exp(model.log_bias):.4f}（应≈0.7）")
    print(f"    区间宽度因子 = {math.exp(2*model.log_halfwidth):.4f}")
    good = abs(math.exp(model.log_bias) - 0.7) < 0.05
    print(f"    {'✓' if good else '✗'} 偏置估计正确")
    ok = ok and good

    # 独立测试集
    n_te = 500
    td = rng.uniform(300, 900, n_te)
    de = td * 0.7 * rng.lognormal(0, 0.15, n_te)
    te_pts = [CalibrationPoint(detected=float(d), truth=float(t), domain="cal")
              for d, t in zip(de, td)]
    rep_in = evaluate_coverage(model, te_pts, "cal")
    print(f"    域内覆盖率 = {rep_in.empirical*100:.1f}% （名义 {rep_in.nominal*100:.0f}%）")
    lo, hi = wilson_interval(int(rep_in.empirical*n_te), n_te)
    print(f"    Wilson 95%CI = [{lo*100:.1f}%, {hi*100:.1f}%]")
    good = rep_in.empirical >= rep_in.nominal - 0.03
    print(f"    {'✓' if good else '✗'} 域内覆盖达标（≥名义-3pp）")
    ok = ok and good

    # --- 4. 跨域：覆盖率应下降 ---
    print("\n[4] 跨域覆盖率（模拟域偏移）")
    # 目标域：偏置变成 0.5（更严重少计），噪声更大
    td2 = rng.uniform(300, 900, n_te)
    de2 = td2 * 0.5 * rng.lognormal(0, 0.30, n_te)
    te_pts2 = [CalibrationPoint(detected=float(d), truth=float(t), domain="other")
               for d, t in zip(de2, td2)]
    rep_out = evaluate_coverage(model, te_pts2, "other")
    print(f"    跨域覆盖率 = {rep_out.empirical*100:.1f}% "
          f"（掉 {rep_out.drop_pp:.1f}pp）")
    good = rep_out.empirical < rep_in.empirical
    print(f"    {'✓' if good else '✗'} 跨域覆盖率下降（这正是论文要找的信号）")
    ok = ok and good

    # --- 5. 分层校准 ---
    print("\n[5] 分层校准（Mondrian）")
    pts = []
    for d, bias in [("A", 0.8), ("B", 0.5)]:
        td3 = rng.uniform(300, 900, 60)
        de3 = td3 * bias * rng.lognormal(0, 0.15, 60)
        pts += [CalibrationPoint(detected=float(dd), truth=float(tt), domain=d)
                for dd, tt in zip(de3, td3)]
    models = calibrate_stratified(pts, alpha=0.10, min_per_domain=5)
    print(f"    域 A 偏置因子 = {math.exp(models['A'].log_bias):.3f}（应≈0.8）")
    print(f"    域 B 偏置因子 = {math.exp(models['B'].log_bias):.3f}（应≈0.5）")
    good = (abs(math.exp(models['A'].log_bias) - 0.8) < 0.1
            and abs(math.exp(models['B'].log_bias) - 0.5) < 0.1)
    print(f"    {'✓' if good else '✗'} 各域独立校准（不同的偏置被分别捕捉）")
    ok = ok and good

    # --- 6. 小样本警示 ---
    print("\n[6] 小样本警示")
    few = [CalibrationPoint(detected=float(d), truth=float(t), domain="tiny")
           for d, t in zip(rng.uniform(300, 900, 4), rng.uniform(300, 900, 4))]
    m_few = calibrate(few, "tiny", alpha=0.10)
    print(f"    n=4 → reliable = {m_few.reliable}（应 False）")
    good = not m_few.reliable
    print(f"    {'✓' if good else '✗'} n<10 被标为不可靠")
    ok = ok and good

    print("\n" + "=" * 70)
    print(f"自检结果：{'全部通过 ✓' if ok else '有失败项 ✗'}")
    print("=" * 70)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="校准与区间")
    ap.add_argument("--selftest", action="store_true", help="自检")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    print("用法：python calibrate.py --selftest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())