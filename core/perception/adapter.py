# -*- coding: utf-8 -*-
"""感知层三模块的输出 → `PerceptionRecord` 契约。

【它解决什么】
    `infer` / `geometry` / `calibrate` 各自产出结构不同的对象，
    下游（门控、报告）不该知道"从哪三个对象里各取哪几个字段"。

【★ 为什么按**属性名**适配，而不是 `import` 他们的 dataclass 当类型】

    感知侧仍在演进（`验证状态.md` 有 5 项待办：Mavic3M/3T 覆盖系数、
    conf/NMS 阈值、分层校准的性能优化…）。若把函数签名绑死在具体类上，
    **他们改一个字段，我们就要跟着改签名**。

    所以本模块只依赖**属性名**（duck typing）：
      · 他们换实现类 → 只要属性名不变，这里不用改；
      · 属性名真的变了 → **这里报出明确的缺失字段名**，而不是一个
        `AttributeError: 'ImageResult' object has no attribute 'tiles'`
        这种看不出因果的错误。

    这与项目里「契约单一真相」的原则一致：**适配器是唯一的转换点**。

【它不做的事】
    · **不做四档判断** —— 那是 `core/uncertainty.py` 的事
    · **不补默认值** —— 拿不到就是 `None`（契约里"未提供"要能表达）
    · **不吞异常** —— 缺字段就报清楚缺什么
"""
from __future__ import annotations

import math
from typing import Any

from core.perception.contract import InferenceSpec, PerceptionRecord


class AdapterError(ValueError):
    """适配失败 —— 消息里必须**点名缺了哪个字段**。"""


# ---------------------------------------------------------------------------
# 取值助手
# ---------------------------------------------------------------------------

_MISSING = object()


def _get(obj: Any, name: str, *, required: bool = False,
         where: str = "") -> Any:
    """按属性名取值；`required=True` 时缺失即抛 `AdapterError`（点名字段）。"""
    if obj is None:
        if required:
            raise AdapterError(f"缺少上游对象 {where or '(未知)'}，无法取 {name}")
        return _MISSING if False else None
    val = getattr(obj, name, _MISSING)
    if val is _MISSING:
        if required:
            raise AdapterError(
                f"{where or type(obj).__name__} 缺少必需字段 {name!r} —— "
                f"请检查感知侧对象结构是否变化")
        return None
    return val


def _f(v: Any) -> float | None:
    """转 float；无法转换或非有限值 → `None`（**不造假**）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


# ---------------------------------------------------------------------------
# ① valid_frame_ratio —— 从 TileInfo 现算（唯一需要"加工"的字段）
# ---------------------------------------------------------------------------


def valid_frame_ratio(image_result: Any) -> float | None:
    """有效帧占比 = 有效切片数 / 总切片数。

    ★ 返回 `None` 而不是 `0.0`：`tiles` 为空意味着**没有信息**，
      而 0 意味着"全部无效"—— 两者语义完全不同，不能混。
    """
    tiles = _get(image_result, "tiles")
    if not tiles:
        return None
    total = len(tiles)
    good = 0
    for t in tiles:
        # 属性名可能是 valid / is_valid，两种都接受（韧性）
        v = getattr(t, "valid", None)
        if v is None:
            v = getattr(t, "is_valid", None)
        if bool(v):
            good += 1
    return good / total if total else None


# ---------------------------------------------------------------------------
# ② 推理协议 —— 从 config 字典取（`config.yaml` 是它的可执行形态）
# ---------------------------------------------------------------------------


def _dict_get(d: Any, *path: str) -> Any:
    """按 `a.b.c` 逐层取字典值；任一环缺失返回 `None`。"""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def inference_spec_from_config(cfg: dict | None) -> InferenceSpec | None:
    """从 `config.yaml` 的字典取五个必需字段。

    ★ 任一必需字段缺失就返回 `None` —— 因为一个**只填了一半的推理协议**
      比"没有协议"更危险：它会让报告看起来"协议齐全"，实际口径不明。
    """
    if not isinstance(cfg, dict):
        return None
    inf = cfg.get("inference") or {}
    need = ("tile_size_px", "tile_overlap", "input_size_px",
            "conf_threshold", "nms_iou")
    if any(inf.get(k) is None for k in need):
        return None
    ver = _dict_get(cfg, "detector", "weights_version") or ""
    try:
        return InferenceSpec(
            tile_size_px=int(inf["tile_size_px"]),
            tile_overlap=float(inf["tile_overlap"]),
            input_size_px=int(inf["input_size_px"]),
            conf_threshold=float(inf["conf_threshold"]),
            nms_iou=float(inf["nms_iou"]),
            weights_version=str(ver),
        )
    except (TypeError, ValueError):
        return None


def detector_id_from_config(cfg: dict | None) -> str | None:
    """检测器标识：优先取 config 的 `detector.id`，退到权重文件名。"""
    if not isinstance(cfg, dict):
        return None
    explicit = _dict_get(cfg, "detector", "id")
    if explicit:
        return str(explicit)
    weights = _dict_get(cfg, "detector", "weights")
    if weights:
        from pathlib import Path
        stem = Path(str(weights)).stem
        return stem or None
    return None


# ---------------------------------------------------------------------------
# ③ 主适配
# ---------------------------------------------------------------------------


def to_record(
    *,
    density_result: Any,
    calibration: Any,
    image_result: Any = None,
    config: dict | None = None,
    field_id: str | None = None,
    date: str | None = None,
    platform: str | None = None,
    detector_id: str | None = None,
) -> PerceptionRecord:
    """把三模块的输出拼成一条完整契约记录。

    Args:
        density_result: `geometry.DensityResult`（必需）—— 密度与覆盖面积
        calibration: `calibrate.CalibrationModel`（必需）—— 区间与校准外键
        image_result: `infer.ImageResult`（可选）—— 用来算 `valid_frame_ratio`
        config: `config.yaml` 解析出的字典（可选）—— 推理协议与检测器标识
        field_id / date / platform: 标识类字段（**拿不到就传 None，不要编**）
        detector_id: 显式指定优先；否则从 config 推

    Raises:
        AdapterError: 缺必需对象 / 缺必需字段（消息里**点名**缺什么）
    """
    if density_result is None:
        raise AdapterError("缺少 density_result —— 没有密度就没有可决策的内容")
    if calibration is None:
        raise AdapterError("缺少 calibration —— 没有校准就没有区间，无从判断可信度")

    density = _f(_get(density_result, "density_per_m2", required=True,
                      where="DensityResult"))
    if density is None:
        raise AdapterError("DensityResult.density_per_m2 不是有效数值")

    # ── 区间：调校准模型自己的 interval()，不在这里重算公式 ──
    # ★ 为什么必须调它的方法：区间公式（log 空间 ± offset）属于校准侧的知识，
    #   在适配器里重写一份就是"两份地图"。
    interval_fn = getattr(calibration, "interval", None)
    if not callable(interval_fn):
        raise AdapterError("CalibrationModel 缺少 interval() 方法，无法得到区间")
    try:
        lo, hi = interval_fn(density)
        lo_f, hi_f = _f(lo), _f(hi)
    except Exception as e:                                   # noqa: BLE001
        raise AdapterError(f"calibration.interval() 执行失败：{e}") from e
    if lo_f is None or hi_f is None:
        raise AdapterError(f"calibration.interval() 返回非数值：({lo!r}, {hi!r})")

    # ── ★★ 点估计用「区间中心」，不是「原始检测密度」──
    #
    # 【这条是被守卫抓出来的】最初直接拿 `DensityResult.density_per_m2`
    #   当 `density_hat_per_m2`，结果校验器报
    #   "点估计 46.30 落在区间 [47.63, 84.09] 之外"。
    #
    #   根因：`density_per_m2` 是**原始检测密度**（检测器少计 25%），
    #   而 §8 的 `density_hat_per_m2` 是**偏置校正后**的估计。
    #   手册例子里 `density_hat=312`、区间 `[268,358]`，中点 313 ≈ 312 —— 印证了这一点。
    #
    #   ★ 取**几何中心** `sqrt(lo*hi)` 而不是 `(lo+hi)/2`：
    #     区间是在 log 空间构造的（`[c-hw, c+hw]`，`c = log(det) - log_bias`），
    #     所以 `exp(c) = sqrt(lo*hi)` 是**构造意义上**的中心，算术中点则在
    #     正偏分布下系统性偏高。
    #
    #   ★ 也不能把 `density_hat` 再喂回 `interval()` —— 那会**重复校正偏置**。
    #     原始检测密度只用于"调 interval()"，校正后的值只用于展示与门控。
    density_hat = math.sqrt(max(lo_f, 0.0) * max(hi_f, 0.0))

    return PerceptionRecord(
        field_id=field_id,
        date=date,
        platform=platform,
        altitude_m=_f(_get(density_result, "altitude_m")),
        detector_id=detector_id or detector_id_from_config(config),
        inference_spec=inference_spec_from_config(config),
        density_hat_per_m2=density_hat,
        density_interval_per_m2=(lo_f, hi_f),
        imaged_area_m2=_f(_get(density_result, "imaged_area_m2")),
        field_area_m2=_f(_get(density_result, "field_area_m2")),
        area_coverage_fraction=_f(_get(density_result, "area_coverage_fraction")),
        valid_frame_ratio=valid_frame_ratio(image_result),
        alpha=_f(_get(calibration, "alpha")),
        calibration_id=_get(calibration, "calibration_id"),
        # ★ 透传小样本警示 —— 不得在适配层"洗白"
        calibration_reliable=_get(calibration, "reliable"),
        calibration_domain=_get(calibration, "domain"),
    )


__all__ = [
    "AdapterError", "detector_id_from_config", "inference_spec_from_config",
    "to_record", "valid_frame_ratio",
]
