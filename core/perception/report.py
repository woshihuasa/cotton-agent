# -*- coding: utf-8 -*-
"""把契约记录 + 门控结论渲染成 Markdown 报告。

【为什么报告必须写清"不确定"这件事】

    这份报告会被拿去**做农业决策**（打不打药、收不收割）。
    如果它只写一个数字和一句结论，读者会当成确定的事实 ——
    而这个数字来自**检测器的估计**，带有校准区间的。

    所以报告有三条硬要求：

      ① **区间必须与点估计一起出现**，不能只给一个数；
      ② **门控的档位与理由必须显式写出**（尤其 ESCALATE / QUALITATIVE）；
      ③ **`reliable=False` 必须显著标注** —— 小样本校准的区间没有统计意义，
         但如果不标注，它看起来和正常区间一模一样。

    ★ 这三条不是"排版偏好"，是**决策可信度**的一部分。

【它不做的事】
    · 不重新计算任何数值（一切来自契约与门控）
    · 不做四档判断（那是 `core/uncertainty.py`）
    · 不隐藏警告（`warnings` 全部原样写出）
"""
from __future__ import annotations

import time
from typing import Any

from core.perception.contract import PerceptionRecord

#: 四档 → 给农技员看的**行为含义**
_LEVEL_MEANING = {
    "ADVISE": "可以据此给正常建议",
    "ADVISE_WITH_INTERVAL": "可以给建议，但**必须同时给出区间**",
    "QUALITATIVE": "只能做**定性**判断，不要给出具体数字结论",
    "ESCALATE": "**信息不足，应弃答**并建议补充观测",
}

_LEVEL_ORDER = ("ESCALATE", "QUALITATIVE", "ADVISE_WITH_INTERVAL", "ADVISE")


def _fmt(v: Any, digits: int = 1, unit: str = "") -> str:
    if v is None:
        return "—（未提供）"
    try:
        return f"{float(v):.{digits}f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def _pct(v: Any, digits: int = 1) -> str:
    if v is None:
        return "—（未提供）"
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(v)


def render(record: PerceptionRecord, decision: Any = None, *,
           title: str | None = None, generated_at: str | None = None,
           extra_notes: list[str] | None = None) -> str:
    """渲染 Markdown 报告。

    Args:
        record: 契约记录
        decision: `core.uncertainty.GateDecision`（可选；无则不写门控段）
        extra_notes: 额外提示（如上游的告警）
    """
    errors, warnings = record.validate()
    lo, hi = record.density_interval_per_m2 or (None, None)
    rel = None
    if lo is not None and hi is not None and record.density_hat_per_m2:
        d = record.density_hat_per_m2
        if d > 0:
            rel = (hi - lo) / d

    name = title or record.field_id or "（未命名田块）"
    ts = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    add = lines.append

    add(f"# 感知计数报告 · {name}")
    add("")
    add(f"> 生成时间：{ts}　·　契约版本：`{record.contract_version}`")
    add("")

    # ---------- ★★★ 合成校准警告（放最前面，不能错过） ----------
    #
    # 【为什么必须在最前面】这份报告是拿去**做农业决策**的。
    #   如果它用的是合成校准，那些数字**没有农学意义** ——
    #   而报告本身看起来完全正常（有区间、有档位、有理由）。
    #   所以"这是演示数据"必须比结论更醒目。
    from core.perception.demo import is_demo
    if is_demo(record.calibration_id):
        add("> # 🔴 本报告使用**合成校准**，数字没有农学意义")
        add("> ")
        add("> 它的用途是**演示机制**（区间 → 四档 → 报告），"
            "不是给出可用的种植建议。")
        add("> 真实校准需要「检测值 ↔ 样方真值（1m×1m 人工计数）」配对。")
        add(f"> 校准 ID：`{record.calibration_id}`")
        add("")

    # ---------- ★ 决策结论放最前（读者最关心的） ----------
    if decision is not None:
        level = getattr(getattr(decision, "level", None), "value",
                        str(getattr(decision, "level", "")))
        side = getattr(getattr(decision, "side", None), "value",
                       str(getattr(decision, "side", "")))
        reason = str(getattr(decision, "reason", "") or "")
        add("## 一、决策结论")
        add("")
        add(f"- **档位**：`{level}`")
        add(f"- **含义**：{_LEVEL_MEANING.get(level, '（未知档位）')}")
        add(f"- **区间与阈值的关系**：`{side}`")
        if reason:
            add(f"- **理由**：{reason}")
        add("")
        if level in ("ESCALATE", "QUALITATIVE"):
            add("> ⚠️ **本结论不足以支撑一个确定的作业决定。**"
                "若必须行动，请先补充观测（增加样方 / 重飞 / 提高影像质量）。")
            add("")
        seg = 2
    else:
        seg = 1

    # ---------- 估计量 ----------
    add(f"## {'二' if seg == 2 else '一'}、估计量")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| 密度点估计 | **{_fmt(record.density_hat_per_m2, 1, ' 个/m²')}** |")
    add(f"| 校准区间 | **[{_fmt(lo, 1)}, {_fmt(hi, 1)}] 个/m²** |")
    add(f"| 区间相对宽度 | {_pct(rel)} |")
    add(f"| 名义覆盖率 (1−α) | {_pct(None if record.alpha is None else 1 - record.alpha)} |")
    add("")
    add("> 区间是**校准出来的**，不是模型自报的置信度。"
        f"它的含义是：真值约有 {(1 - record.alpha) * 100:.0f}% 的概率落在其中"
        if record.alpha is not None else
        "> 区间是**校准出来的**，不是模型自报的置信度。")
    add("")

    # ---------- 覆盖面 ----------
    add("## " + ("三" if seg == 2 else "二") + "、覆盖面与影像质量")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| 已拍面积 | {_fmt(record.imaged_area_m2, 2, ' m²')} |")
    add(f"| 田块面积 | {_fmt(record.field_area_m2, 2, ' m²')} |")
    add(f"| 面积覆盖率 | {_pct(record.area_coverage_fraction)} |")
    add(f"| 有效帧占比 | {_pct(record.valid_frame_ratio)} |")
    add(f"| 飞行高度 | {_fmt(record.altitude_m, 1, ' m')} |")
    add("")
    if record.area_coverage_fraction is not None and \
            record.area_coverage_fraction < 0.5:
        add("> ⚠️ **影像只覆盖了不到一半的田块。**"
            "低密度可能来自「没拍全」，而不是「棉絮少」—— 下结论前需确认。")
        add("")

    # ---------- 校准与可复现性 ----------
    add("## " + ("四" if seg == 2 else "三") + "、校准与可复现性")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| 校准 ID（外键） | `{record.calibration_id or '—'}` |")
    add(f"| 校准域 | `{record.calibration_domain or '—'}` |")
    add(f"| 校准样本量可靠 | "
        f"{'✅ 是' if record.calibration_reliable else ('❌ **否（样本不足）**' if record.calibration_reliable is False else '—（未知）')} |")
    add(f"| 检测器 | `{record.detector_id or '—'}` |")
    add(f"| 平台 | {record.platform or '—'} |")
    add(f"| 日期 | {record.date or '—'} |")
    add("")
    if record.calibration_reliable is False:
        add("> 🔴 **校准样本量不足（n < 10）。这个区间没有统计意义** —— "
            "上述区间宽度不可用于判断可信度，请先补齐校准数据。")
        add("")

    spec = record.inference_spec
    if spec is not None:
        add("**推理协议**（不钉住它，结果不可复现）")
        add("")
        add("| 参数 | 值 |")
        add("|---|---|")
        sd = spec.to_dict()
        add(f"| 切片边长 | {sd['tile_size_px']} px |")
        add(f"| 切片重叠 | {sd['tile_overlap']} |")
        add(f"| 模型输入 | {sd['input_size_px']} px |")
        add(f"| 置信度阈值 | {sd['conf_threshold']} |")
        add(f"| NMS IoU | {sd['nms_iou']} |")
        add(f"| 权重版本 | `{sd['weights_version'] or '—'}` |")
        add("")

    # ---------- 警告 ----------
    notes = list(extra_notes or [])
    if errors or warnings:
        add("## " + ("五" if seg == 2 else "四") + "、数据完整性与警告")
        add("")
        for e in errors:
            add(f"- 🔴 **错误**：{e}")
        for w in warnings:
            add(f"- ⚠️ {w}")
        for n in notes:
            add(f"- ℹ️ {n}")
        add("")

    # ---------- 边界声明 ----------
    add("---")
    add("")
    add("## 边界声明（必读）")
    add("")
    add("1. **本报告不含真实 YOLO 推理**（V1 边界）：输入是**已检测出的框数**，"
        "不是影像。检测器环节由上游提供。")
    add("2. **密度是估计量，不是真值**。区间之外仍可能有真值（"
        f"名义覆盖率 {(1 - record.alpha) * 100:.0f}% 意味着每 100 次约 "
        f"{round(record.alpha * 100)} 次会漏掉）" if record.alpha is not None else
        "2. **密度是估计量，不是真值。**")
    add("3. **区间宽度受校准域限制**：跨机型/跨高度时覆盖率会下降，"
        "不能直接把本报告结论搬到另一批影像上。")
    add("")
    return "\n".join(lines)


def write_report(record: PerceptionRecord, decision: Any = None,
                 out_dir: str | Path = "reports", **kwargs) -> Path:
    """渲染并落盘，返回文件路径。

    ★ 默认落在**项目内** `reports/` —— 本版不碰用户工作区
      （理由见 `docs/PERCEPTION.md` §六：工作区是新的信任面，需配套强制机制）。
    """
    from pathlib import Path as _P

    d = _P(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stem = record.field_id or "perception"
    ts = time.strftime("%Y%m%d-%H%M%S")
    p = d / f"{stem}-{ts}.md"
    p.write_text(render(record, decision, **kwargs), encoding="utf-8")
    return p


__all__ = ["render", "write_report"]
