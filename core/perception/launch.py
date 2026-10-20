# -*- coding: utf-8 -*-
"""从对话指令启动感知任务（`/perception` 的执行侧）。

【它解决什么】

    在此之前，`/perception` 只做一件事：**跳过 handoff，然后打印一段提示**。
    真正的工作流要人工去编排层跑 —— 等于"入口"是假的。

    本模块把它接上：解析指令 → 组装工作流输入 → **后台启动**
    `workflows/perception_report.yaml` → 返回 run_id 让上层回填聊天。

【★ 两条路径，泾渭分明】

    · **真实路径**（默认）：必须给 `calibration_path`（真实校准文件）
      与 `n_boxes` / `altitude_m` / `threshold`。缺任何一项都**不启动**，
      而是告诉用户缺什么。
    · **演示路径**（`--demo`）：用**合成校准** + 示例数字跑一遍，
      **必须显式索取**。合成校准的 `calibration_id` 带 `cal-demo-` 前缀，
      报告里会打红框警告。

    ★ 为什么演示要显式：默认路径悄悄用合成数据，等于**用假数据演示真机制** ——
      看的人会以为那是真实结果。显式索取把"我知道这是演示"变成用户的责任。

【设计约束】
    · **只有一处组装输入的逻辑**（`_assemble`）—— 两份必然对不上。
    · `build_inputs` 是**纯函数**（不碰磁盘、不起线程），便于单测。
    · 只有 `launch()` 会写盘与启动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

def _workflow_path() -> Path:
    """工作流文件路径 —— **必须**走 `resource_path`，不能依赖 cwd。

    ★ 2026-10-20 修：第一版是 `Path("workflows/perception_report.yaml")`，
      相对路径按**进程 cwd** 解析。用户从别的目录启动（`python E:/x/main.py`）
      就找不到；打包后更是完全指错（包内资源在 `_MEIPASS` 下）。
      实测后果：打包版 `/perception` 直接报「工作流文件不存在」。
    """
    from config import resource_path
    return resource_path("workflows/perception_report.yaml")


#: 兼容旧引用（测试与文档里读的是这个名字）。
#: ★ 用模块级函数而不是常量：常量在 import 时求值，测试里想 monkeypatch
#:   cwd / _MEIPASS 就没机会了。
def _get_workflow_path() -> Path:
    return _workflow_path()

#: 真实路径必需的指令参数
REQUIRED_PARAMS = ("n_boxes", "altitude_m", "threshold", "calibration_path")

#: 演示路径的默认值（**明确是示例数字**）
DEMO_DEFAULTS: dict[str, Any] = {
    "field_id": "DEMO-F01",
    "n_boxes": 1414,
    "altitude_m": 5.0,
    "threshold": 75.0,
    "camera": "Mavic3Classic",
}

#: 指令里可直接透传给工作流的**字符串型**参数
_STR_KEYS = ("camera", "date", "platform", "detector_id", "field_id")
#: 指令里可直接透传的**数值型**参数
_NUM_KEYS = ("altitude_m", "threshold", "field_area_m2")
#: 指令里可直接透传的**整数型**参数
_INT_KEYS = ("n_boxes", "invalid_tiles")


@dataclass
class LaunchResult:
    """一次启动尝试的结果。"""

    ok: bool
    run_id: str = ""
    demo: bool = False
    message: str = ""
    #: 缺失的必需参数（失败时用）
    missing: list[str] = field(default_factory=list)
    #: ★ 组装好的工作流输入（成功时才有）—— 由 `build_inputs` 填
    inputs: dict = field(default_factory=dict)


def _is_demo(params: dict, raw: str) -> bool:
    """这条指令是不是要走演示路径。"""
    return ("--demo" in raw
            or params.get("demo") is not None
            or params.get("演示") is not None)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _assemble(params: dict, demo: bool, out_dir: str | Path) -> dict:
    """**唯一**的输入组装逻辑（两条路径共用同一份键名处理）。"""
    out = str(out_dir)
    if demo:
        inputs: dict[str, Any] = dict(DEMO_DEFAULTS)
        inputs["calibration_path"] = str(Path(out) / "demo_calibration.json")
        inputs["out_dir"] = out
    else:
        inputs = {
            "field_id": str(params.get("field_id") or "UNKNOWN"),
            "calibration_path": str(params.get("calibration_path") or ""),
            "out_dir": out,
        }

    for k in _STR_KEYS:
        if params.get(k):
            inputs[k] = str(params[k])
    for k in _NUM_KEYS:
        if params.get(k) is not None:
            got = _num(params[k])
            if got is not None:
                inputs[k] = got
    for k in _INT_KEYS:
        if params.get(k) is not None:
            got = _num(params[k])
            if got is not None:
                inputs[k] = int(got)
    return inputs


def build_inputs(request: Any, *, out_dir: str | Path = "reports") -> LaunchResult:
    """把一条 `/perception` 指令解析成工作流输入。

    ★ **纯函数**：不写盘、不起线程、不 import 重模块。便于单测。
    """
    params = dict(getattr(request, "params", {}) or {})
    raw = str(getattr(request, "raw", "") or "")
    demo = _is_demo(params, raw)

    if not demo:
        missing = [k for k in REQUIRED_PARAMS if not params.get(k)]
        if missing:
            return LaunchResult(
                False, missing=missing,
                message="缺少必需参数：" + "、".join(f"`{m}`" for m in missing))

    inputs = _assemble(params, demo, out_dir)
    return LaunchResult(True, demo=demo, inputs=inputs,
                        message="演示路径（**合成**校准）" if demo else "真实校准")


def launch(request: Any, *, runner: Any = None,
           out_dir: str | Path = "reports") -> LaunchResult:
    """解析 + **后台启动**感知工作流。

    ★ 用与 handoff 同一个 `TaskRunner`（`core.handoff.get_runner()`）——
      两者都是"主链路产出的任务"，共用一个执行器才能统一追踪。
    """
    prepared = build_inputs(request, out_dir=out_dir)
    if not prepared.ok:
        return prepared

    # ① 演示路径：先落一份合成校准（**显式索取才走到这里**）
    if prepared.demo:
        try:
            from core.perception.demo import write_demo_calibration
            write_demo_calibration(prepared.inputs["calibration_path"])
        except Exception as e:                               # noqa: BLE001
            return LaunchResult(False, demo=True,
                                message=f"生成演示校准失败：{type(e).__name__}: {e}")

    wf_path = _workflow_path()
    if not wf_path.is_file():
        return LaunchResult(False, demo=prepared.demo,
                            message=f"工作流文件不存在：{wf_path}")

    # ② 后台启动（与 handoff 共用执行器）
    try:
        if runner is None:
            from core import handoff as _h
            runner = _h.get_runner()
        yaml_text = wf_path.read_text(encoding="utf-8")
        handle = runner.submit_yaml(yaml_text, prepared.inputs)
    except Exception as e:                                   # noqa: BLE001
        return LaunchResult(False, demo=prepared.demo,
                            message=f"启动失败：{type(e).__name__}: {e}")

    if not getattr(handle, "launched", False):
        issues = list(getattr(handle, "issues", []) or [])
        return LaunchResult(
            False, demo=prepared.demo, inputs=prepared.inputs,
            message="工作流未启动：" + ("；".join(issues[:3]) or "未知原因"))

    return LaunchResult(True, run_id=str(handle.run_id), demo=prepared.demo,
                        inputs=prepared.inputs, message=prepared.message)


def help_text() -> str:
    """`/perception` 的用法说明（缺参数时回给用户）。"""
    return (
        "**感知任务用法**\n\n"
        "```\n"
        "/perception field_id=2025-F03 n_boxes=1414 altitude_m=5 "
        "threshold=75 calibration_path=cal.json\n"
        "/perception --demo        ← 用**合成**校准跑一遍（演示机制用）\n"
        "```\n\n"
        "**必需参数**：`n_boxes`（已检测框数）、`altitude_m`（飞行高度）、"
        "`threshold`（决策阈值）、`calibration_path`（校准模型 JSON）。\n\n"
        "★ 校准文件从哪来：需要「检测值 ↔ 样方真值」配对，"
        "用 `core.perception.calibrate.calibrate()` 算出后 `to_dict()` 落盘。\n"
        "★ 没有真实数据时可用 `--demo`（合成校准，**报告里会标注**）。"
    )


#: 等报告文件的默认超时（秒）。
#:
#: ★ 为什么可以等：`perception_report` 三个节点**全是本地计算**
#: （切片规划 / 密度换算 / 区间 / Markdown 渲染），**不调 LLM**，
#: 实测 1–2 秒完成。所以"发完指令顺手把报告路径带回去"是可行的 ——
#: 用户点一下就能看到报告，而不是再去文件夹里翻。
#:
#: ★ 超时了怎么办：**如实说"还在跑"**，并给出目录，绝不假装已完成。
DEFAULT_REPORT_TIMEOUT = 15.0


def wait_for_report(run_id: str, *, runner: Any = None,
                    timeout: float = DEFAULT_REPORT_TIMEOUT) -> str:
    """等任务跑完，返回报告文件的**绝对路径**；未完成/失败 → 空串。

    ★ 只认 `report` 节点的产出，且**必须文件真的存在**才返回 ——
      "节点说成功但文件不在"正是本项目反复强调的静默失败形态。
    """
    if not run_id:
        return ""
    try:
        if runner is None:
            from core import handoff as _h
            runner = _h.get_runner()
        run = runner.wait(run_id, timeout=timeout)
    except Exception:                                        # noqa: BLE001
        return ""
    if run is None:
        return ""
    steps = getattr(run, "steps", {}) or {}
    out = getattr(steps.get("report"), "output", None) or {}
    if not isinstance(out, dict):
        return ""
    raw = str(out.get("path") or "")
    if not raw:
        return ""
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return str(p) if p.is_file() else ""


__all__ = [
    "DEFAULT_REPORT_TIMEOUT", "DEMO_DEFAULTS", "LaunchResult",
    "REQUIRED_PARAMS", "_workflow_path", "build_inputs", "help_text",
    "launch", "wait_for_report",
]