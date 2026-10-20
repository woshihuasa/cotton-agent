# -*- coding: utf-8 -*-
"""感知管线 —— 把「框数 + 影像元数据 + 校准模型」跑成一条契约记录。

【V1 的边界（必须说清）】

    ★ **V1 不跑 YOLO 推理。** 真推理要 `ultralytics` + `.pt` 权重（含 torch，体量大），
      不在本版范围。所以本模块的入口是 **`n_boxes`（已检测出的框数）**，
      而不是影像路径。

    这样切分的好处：**除了"检测器"那一环，其余全部是真的** ——
      切片规划（`infer.plan_tiles`，纯几何）、
      密度换算（`geometry.compute_density`，纯数学）、
      区间校准（`calibrate`，纯 numpy）、
      契约拼装（`adapter`）、门控（`core.uncertainty`）、报告。

    V2 只需在本模块前面加一个"影像 → 框数"的步骤（调 `infer.infer_image`），
    **下游一行都不用改**。

【为什么要规划切片而不是直接算面积】

    `geometry.compute_density` 需要 `tiles` 来表达"重叠面积只算一次"。
    朴素做法（片数 × 单片面积）会因 15% 重叠而虚高约 60%（见其自检第 4 节），
    分母一虚高，密度就被系统性低估。

【校准模型从哪来】

    **必须显式提供**（一个 JSON：`CalibrationModel.to_dict()` 的输出）。
    拿不到就**报错**，不内置"演示用"的假校准 —— 假区间会一路流进门控，
    让系统做出看起来有依据、实际无依据的决策。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from core.perception import geometry as _geom
from core.perception import infer as _infer
from core.perception.adapter import to_record
from core.perception.calibrate import CalibrationModel
from core.perception.contract import PerceptionRecord


class PipelineError(ValueError):
    """管线无法继续 —— 消息必须说清缺什么。"""


# ---------------------------------------------------------------------------
# 校准模型载入
# ---------------------------------------------------------------------------


def load_calibration(path: str | Path) -> CalibrationModel:
    """从 JSON 载入校准模型（`CalibrationModel.to_dict()` 的输出）。

    ★ 只接受**完整**的模型：缺 `log_bias` / `log_halfwidth` / `alpha` 就报错。
      理由：一个填了一半的校准模型会让区间看起来"有来源"，实际不可复现。
    """
    p = Path(path)
    if not p.is_file():
        raise PipelineError(f"校准文件不存在：{p}")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise PipelineError(f"校准文件无法读取：{p} —— {e}") from e
    if not isinstance(d, dict):
        raise PipelineError(f"校准文件格式错误（应为对象）：{p}")

    # 允许包一层 {"models": {...}} 或直接是单条
    if "models" in d and isinstance(d["models"], dict):
        # 取第一条（调用方若要多域，应显式传 domain）
        first = next(iter(d["models"].values()), None)
        if not isinstance(first, dict):
            raise PipelineError(f"校准文件里没有可用的模型：{p}")
        d = first

    need = ("calibration_id", "alpha", "log_bias", "log_halfwidth")
    missing = [k for k in need if d.get(k) is None]
    if missing:
        raise PipelineError(
            f"校准文件缺少必需字段 {missing} —— 无法构造可用区间（{p}）")

    try:
        return CalibrationModel(
            calibration_id=str(d["calibration_id"]),
            domain=str(d.get("domain") or "default"),
            alpha=float(d["alpha"]),
            n_cal=int(d.get("n_cal") or 0),
            log_bias=float(d["log_bias"]),
            log_halfwidth=float(d["log_halfwidth"]),
            empirical_coverage=float(d.get("empirical_coverage") or 0.0),
            method=str(d.get("method") or "split_conformal_log"),
            reliable=bool(d.get("reliable", True)),
        )
    except (TypeError, ValueError) as e:
        raise PipelineError(f"校准文件字段类型错误：{e}") from e


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_record(
    *,
    n_boxes: int,
    altitude_m: float,
    calibration: CalibrationModel,
    camera: str = "Mavic3Classic",
    image_width_px: int | None = None,
    image_height_px: int | None = None,
    tile_size_px: int = 640,
    tile_overlap: float = 0.15,
    input_size_px: int = 640,
    conf_threshold: float = 0.25,
    nms_iou: float = 0.5,
    weights_version: str = "",
    field_id: str | None = None,
    date: str | None = None,
    platform: str | None = None,
    detector_id: str | None = None,
    field_area_m2: float | None = None,
    invalid_tiles: int = 0,
) -> PerceptionRecord:
    """跑完整条链：切片规划 → 密度 → 契约。

    Args:
        n_boxes: **已检测出的框数**（V1 的输入；V2 由 `infer.infer_image` 产出）
        altitude_m: 飞行高度（米）—— 它决定 GSD，进而决定单片覆盖面积
        calibration: 校准模型（区间来源）
        camera: `geometry.CAMERAS` 的关键字（机型覆盖率参数）
        invalid_tiles: 判为无效（过曝/模糊）的切片数 —— 用于 `valid_frame_ratio`

    Raises:
        PipelineError: 参数不可用 / 无法算出有效密度
    """
    if n_boxes < 0:
        raise PipelineError(f"框数不能为负：{n_boxes}")
    if altitude_m is None or not (float(altitude_m) > 0):
        raise PipelineError(f"飞行高度必须为正：{altitude_m!r}")

    cam = _geom.CAMERAS.get(str(camera))
    if cam is None:
        raise PipelineError(
            f"未知机型 {camera!r}；可用：{sorted(_geom.CAMERAS)}")

    w = int(image_width_px or cam.image_width_px)
    h = int(image_height_px or cam.image_height_px)

    # ① 切片规划（纯几何，无 cv2）
    boxes_xy = _infer.plan_tiles(w, h, tile_size=int(tile_size_px),
                                 overlap=float(tile_overlap))
    if not boxes_xy:
        raise PipelineError("切片规划为空 —— 影像尺寸或切片边长不合理")

    # ② 组装 tiles（`compute_density` 用它表达"重叠只算一次"）
    n_invalid = max(0, min(int(invalid_tiles), len(boxes_xy)))
    tiles: list[dict] = []
    for i, (x0, y0, x1, y1) in enumerate(boxes_xy):
        tiles.append({
            "tile_id": i, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            # 前 n_invalid 片标为无效（V1 无质量判定，由调用方告知）
            "valid": i >= n_invalid,
            "n_boxes": 0,
        })

    # ③ 密度
    try:
        dr = _geom.compute_density(
            n_boxes=int(n_boxes), tiles=tiles, camera=cam,
            altitude_m=float(altitude_m), tile_size_px=int(tile_size_px),
            field_area_m2=field_area_m2)
    except Exception as e:                                   # noqa: BLE001
        raise PipelineError(f"密度换算失败：{type(e).__name__}: {e}") from e

    if not (dr.density_per_m2 > 0) and n_boxes > 0:
        raise PipelineError(
            f"密度为 {dr.density_per_m2}（框数 {n_boxes}，"
            f"覆盖 {dr.imaged_area_m2} m²）—— 面积无效")

    # ④ 契约（区间由 adapter 调 calibration.interval() 得到）
    cfg = {
        "detector": {"weights_version": weights_version,
                     "id": detector_id or ""},
        "inference": {
            "tile_size_px": tile_size_px,
            "tile_overlap": tile_overlap,
            "input_size_px": input_size_px,
            "conf_threshold": conf_threshold,
            "nms_iou": nms_iou,
        },
    }

    # 造一个"像 ImageResult"的薄壳，供 adapter 算 valid_frame_ratio
    class _Img:
        def __init__(self, ts):
            self.tiles = ts

    class _Tile:
        def __init__(self, ok):
            self.valid = ok

    shell = _Img([_Tile(bool(t["valid"])) for t in tiles])

    record = to_record(
        density_result=dr, calibration=calibration, image_result=shell,
        config=cfg, field_id=field_id, date=date,
        platform=platform or str(camera), detector_id=detector_id or None,
    )
    return record


def describe_interval(record: PerceptionRecord) -> dict:
    """区间与相对宽度 —— 门控判"宽窄"与报告展示都要它。

    ★ 相对宽度用 `(hi - lo) / density_hat`（与 `core/uncertainty.py` 的口径一致）。
    """
    if not record.density_interval_per_m2:
        return {"lo": None, "hi": None, "width": None, "rel_width": None}
    lo, hi = record.density_interval_per_m2
    w = hi - lo
    d = record.density_hat_per_m2 or 0.0
    rel = (w / d) if d and math.isfinite(d) and d > 0 else None
    return {"lo": lo, "hi": hi, "width": w,
            "rel_width": round(rel, 4) if rel is not None else None}


__all__ = [
    "PipelineError", "build_record", "describe_interval", "load_calibration",
]