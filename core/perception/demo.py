# -*- coding: utf-8 -*-
"""演示用校准模型 —— **明确标注是合成的**。

【为什么需要它】

    感知链路的"可信度控制"要能当场演示：区间跨阈值 → 弃答 → 报告写明信息不足。
    但真实校准需要**样方真值**（1m×1m 人工计数），那是论文侧的数据，不在代码库里。

    所以这里提供一份**合成**校准，让机制可以被演示。

【★★ 绝不假装这是真的 —— 三条防线】

    ① **必须显式索取**：只有 `/perception --demo` 才会用到它，
       正常路径（给了 `calibration_path`）永远走真实校准。
    ② **`calibration_id` 自带标记**：`cal-demo-synthetic-*`，
       落进契约、落进报告，**可被任何下游识别**。
    ③ **报告里显著警告**：`report.py` 见到 `cal-demo` 前缀会打红框。

【它合成的是什么】

    模拟一个「系统性少计 + 乘性噪声」的检测器：
      · 真值 truth
      · 检测值 = truth × bias × exp(N(0, sigma))
    这正是 NMS 抑制相邻框的表现形态（密度越高漏越多 → 乘性），
    所以校准出来的区间形态与真实情形**同构**，只是数字没有农学意义。
"""
from __future__ import annotations

from pathlib import Path

from core.perception.calibrate import CalibrationModel, CalibrationPoint, calibrate

#: 演示校准的 ID 前缀 —— 下游据此识别（报告会打警告）
DEMO_PREFIX = "cal-demo-synthetic"

#: 默认的合成参数（改这些会让演示数字变化，但形态不变）
DEFAULT_BIAS = 0.75        # 系统性少计 25%
DEFAULT_SIGMA = 0.18       # 乘性噪声
DEFAULT_N = 80             # 校准点数量
DEFAULT_ALPHA = 0.10       # 名义覆盖率 90%


def make_demo_calibration(
    *, seed: int = 7, n: int = DEFAULT_N, bias: float = DEFAULT_BIAS,
    sigma: float = DEFAULT_SIGMA, alpha: float = DEFAULT_ALPHA,
    domain: str = "demo-synthetic",
) -> CalibrationModel:
    """造一份**合成**校准模型。

    ★ 用固定的默认 seed —— 这样演示是**可复现**的（换个 seed 数字会变）。
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    truth = rng.uniform(50.0, 200.0, int(n))
    detected = truth * float(bias) * np.exp(rng.normal(0.0, float(sigma), int(n)))

    points = [CalibrationPoint(detected=float(d), truth=float(t))
              for d, t in zip(detected, truth)]
    model = calibrate(points, domain=domain, alpha=float(alpha))

    # ★ 覆盖 calibration_id —— 让"这是合成的"跟着模型一路走
    model.calibration_id = f"{DEMO_PREFIX}-n{int(n)}"
    return model


def write_demo_calibration(path: str | Path, **kwargs) -> Path:
    """把合成校准写到 JSON（`CalibrationModel.to_dict()` 的格式）。"""
    import json

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    model = make_demo_calibration(**kwargs)
    payload = model.to_dict()
    # ★ 额外写一个说明字段 —— 人打开文件就知道这不是真实校准
    payload["_note"] = ("这是**合成**校准，仅用于演示感知链路的机制"
                       "（区间→四档→报告）。数字没有农学意义。"
                       "真实校准需要样方真值（1m×1m 人工计数）。")
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def is_demo(calibration_id: str | None) -> bool:
    """这个 calibration_id 是不是演示用的。"""
    return bool(calibration_id) and str(calibration_id).startswith(DEMO_PREFIX)


__all__ = [
    "DEFAULT_ALPHA", "DEFAULT_BIAS", "DEFAULT_N", "DEFAULT_SIGMA",
    "DEMO_PREFIX", "is_demo", "make_demo_calibration",
    "write_demo_calibration",
]


if __name__ == "__main__":                     # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(
        description="生成一份**合成**校准模型（仅用于演示机制，数字无农学意义）")
    ap.add_argument("--out", default="demo_calibration.json",
                    help="输出 JSON 路径")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--bias", type=float, default=DEFAULT_BIAS)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    args = ap.parse_args()

    out = write_demo_calibration(
        args.out, seed=args.seed, n=args.n, bias=args.bias,
        sigma=args.sigma, alpha=args.alpha)
    print(f"已写出合成校准：{out}")
    print("★ 提醒：这是**演示数据**，不是真实校准 —— 报告里会标注。")