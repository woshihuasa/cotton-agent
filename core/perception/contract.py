# -*- coding: utf-8 -*-
"""感知层输出契约 —— `测控路线操作手册 §8` 的**可执行形态**。

【为什么需要它】
    感知侧三个模块（`infer` / `geometry` / `calibrate`）各自产出**结构不同**的对象：
    `ImageResult` / `DensityResult` / `CalibrationModel`。
    而下游（门控、报告、编排层）需要的是一条**完整、可序列化、字段含义确定**的记录。

    没有这层契约，每个消费方都得自己知道"从哪三个对象里各取哪几个字段" ——
    那就是**多份地图**，必然对不上。本项目已经为此栽过（工具定义的"两份地图"）。

【★ 三个设计原则，都是被追问过的】

    ① **用 `None` 明确表达"未提供"，绝不填默认值**
       `field_id=None` 必须能被表达。填个 `"unknown"` 会让报告无法追溯到具体田块，
       而"看起来正常"的假编号比缺失更危险。

    ② **`calibration_id` 是外键**
       覆盖率是**校准程序的属性**，不是单条记录的属性（§8 原话）。
       所以本契约只存 id，不存覆盖率数值 —— 数值在校准表里，可被审计。

    ③ **区分 error 与 warning**
       缺 `field_id` 是**警告**（报告仍可出，只是不能追溯到田块）；
       缺 `density` 或区间是**错误**（那就没有可决策的内容了）。
       一律当错误的后果是"永远出不了报告"；一律当警告的后果是"出空报告"。

【与 `core/uncertainty.py` 的关系】
    本契约产出 `density_hat_per_m2` 与 `density_interval_per_m2`，
    **那正是 `uncertainty.evaluate(lo=..., hi=..., threshold=...)` 的入参**。
    本模块**不**做四档判断 —— 那是门控的事，不是契约的事。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: 契约版本。字段语义变化时递增（供报告与校准表对齐）。
CONTRACT_VERSION = "perception-contract/v1"


# ---------------------------------------------------------------------------
# 子结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceSpec:
    """推理协议 —— **黑箱不等于可以不钉协议**。

    ★ 为什么这五个字段必须进契约：同一检测器"裁切 640"与"缩放 640"
      相差 8 倍分辨率（见 `研究方向探索.md`）。不钉住协议，结果不可复现。
    """

    tile_size_px: int
    tile_overlap: float
    input_size_px: int
    conf_threshold: float
    nms_iou: float
    weights_version: str = ""

    def to_dict(self) -> dict:
        return {
            "tile_size_px": int(self.tile_size_px),
            "tile_overlap": round(float(self.tile_overlap), 4),
            "input_size_px": int(self.input_size_px),
            "conf_threshold": round(float(self.conf_threshold), 4),
            "nms_iou": round(float(self.nms_iou), 4),
            "weights_version": str(self.weights_version or ""),
        }


# ---------------------------------------------------------------------------
# 主契约
# ---------------------------------------------------------------------------


@dataclass
class PerceptionRecord:
    """一次感知的完整记录 —— `§8` 的 15 个顶层字段。

    默认值全是 `None`，**含义是"未提供"而不是"零"**。
    """

    # --- 标识（谁、哪块地、什么时候）---
    field_id: str | None = None
    date: str | None = None
    platform: str | None = None
    altitude_m: float | None = None
    detector_id: str | None = None
    inference_spec: InferenceSpec | None = None

    # --- 估计量（带分母的密度，不是裸计数）---
    #: 点估计（个/m²）—— **必须带分母**：5m 和 9m 的同一个框数不是同一个量
    density_hat_per_m2: float | None = None
    #: 校准区间 `(lo, hi)` —— 由 `CalibrationModel.interval()` 产出
    density_interval_per_m2: tuple[float, float] | None = None

    # --- 覆盖面（低计数可能来自"只拍了一半"，不是"漏喷"）---
    imaged_area_m2: float | None = None
    field_area_m2: float | None = None
    #: 已拍面积 / 田块总面积
    area_coverage_fraction: float | None = None
    #: 有效帧占比（剔过曝/模糊/零检出后的比例）
    valid_frame_ratio: float | None = None

    # --- 校准（覆盖率是**校准程序的属性**）---
    alpha: float | None = None
    #: 外键 —— 指向校准表；本记录**不存**覆盖率数值
    calibration_id: str | None = None
    #: ★ 小样本警示（n < 10 时区间没有意义），从 `CalibrationModel.reliable` 透传
    calibration_reliable: bool | None = None
    #: 校准域（如 `mavic3m-5m`）—— 报告必须显示，用户要知道区间在哪批数据上校准的
    calibration_domain: str | None = None

    # --- 版本 ---
    contract_version: str = CONTRACT_VERSION

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """转成纯 JSON 可序列化结构 —— 报告与断点落盘都要它。"""
        d: dict[str, Any] = {
            "contract_version": self.contract_version,
            "field_id": self.field_id,
            "date": self.date,
            "platform": self.platform,
            "altitude_m": _num(self.altitude_m),
            "detector_id": self.detector_id,
            "inference_spec": (self.inference_spec.to_dict()
                               if self.inference_spec else None),
            "density_hat_per_m2": _num(self.density_hat_per_m2),
            "density_interval_per_m2": (
                [_num(self.density_interval_per_m2[0]),
                 _num(self.density_interval_per_m2[1])]
                if self.density_interval_per_m2 else None),
            "imaged_area_m2": _num(self.imaged_area_m2),
            "field_area_m2": _num(self.field_area_m2),
            "area_coverage_fraction": _num(self.area_coverage_fraction),
            "valid_frame_ratio": _num(self.valid_frame_ratio),
            "alpha": _num(self.alpha),
            "calibration_id": self.calibration_id,
            "calibration_reliable": self.calibration_reliable,
            "calibration_domain": self.calibration_domain,
        }
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def validate(self) -> tuple[list[str], list[str]]:
        """返回 `(errors, warnings)`。

        ★ **区分两类是本契约的核心判断**（见模块注释原则 ③）：

          · `errors`   —— 缺了它就没有可决策的内容（密度 / 区间 / alpha）
          · `warnings` —— 缺了它报告仍可出，但**可追溯性或可解释性下降**
        """
        errors: list[str] = []
        warnings: list[str] = []

        # ---- errors：没有这些就没有可决策的东西 ----
        if self.density_hat_per_m2 is None:
            errors.append("缺少密度点估计（density_hat_per_m2）")
        elif not (self.density_hat_per_m2 > 0):
            errors.append(f"密度必须为正，实际 {self.density_hat_per_m2!r}")

        if self.density_interval_per_m2 is None:
            errors.append("缺少密度区间（density_interval_per_m2）—— 门控无从判断")
        else:
            lo, hi = self.density_interval_per_m2
            if not lo <= hi:
                errors.append(f"区间反向：lo={lo} > hi={hi}")
            if lo < 0:
                errors.append(f"区间下界为负：{lo}")
            if self.density_hat_per_m2 is not None:
                # 点估计应落在区间内（校准区间是围绕点估计构造的）
                if not (lo - 1e-9 <= self.density_hat_per_m2 <= hi + 1e-9):
                    warnings.append(
                        f"点估计 {self.density_hat_per_m2:.2f} 落在区间 "
                        f"[{lo:.2f}, {hi:.2f}] 之外 —— 请检查校准模型与输入是否配套")

        if self.alpha is None:
            errors.append("缺少名义覆盖率 alpha —— 无法说明区间保证")
        elif not (0 < self.alpha < 1):
            errors.append(f"alpha 必须在 (0,1)，实际 {self.alpha!r}")

        if not self.calibration_id:
            errors.append("缺少 calibration_id —— 区间无法追溯到校准程序")

        # ---- warnings：可解释性下降，但不阻塞 ----
        if not self.field_id:
            warnings.append("缺少 field_id —— 报告无法追溯到具体田块")
        if not self.date:
            warnings.append("缺少 date —— 无法判断影像时效")
        if not self.platform:
            warnings.append("缺少 platform —— 无法说明是哪台设备拍的")
        if not self.detector_id:
            warnings.append("缺少 detector_id —— 结果不可复现")
        if self.inference_spec is None:
            warnings.append("缺少 inference_spec —— 推理协议未钉住，结果不可复现")
        if self.calibration_reliable is False:
            warnings.append(
                "★ 校准样本量不足（reliable=False）—— 区间没有统计意义，"
                "报告必须显式标注")
        if self.area_coverage_fraction is not None and \
                self.area_coverage_fraction < 0.5:
            warnings.append(
                f"影像只覆盖 {self.area_coverage_fraction:.0%} 的田块 —— "
                "低密度可能来自'没拍全'，而不是'棉絮少'")

        return errors, warnings

    @property
    def is_decidable(self) -> bool:
        """能否拿去做决策（只有 errors 为空才行）。"""
        return not self.validate()[0]


def _num(v: Any) -> Any:
    """把数值规整成 JSON 友好的形式；`None` 原样保留（表示"未提供"）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 6)


__all__ = [
    "CONTRACT_VERSION", "InferenceSpec", "PerceptionRecord",
]
