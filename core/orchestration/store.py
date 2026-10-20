# -*- coding: utf-8 -*-
"""运行态与断点持久化 —— **中断后可续跑，且不重复执行已完成的节点**。

【存在哪】`app_data_dir()/workflows/runs/<run_id>.json`
        复用项目现有的"三区路径分离"（配置/运行数据/用户数据），不新造目录约定。

【★ 原子写入】先写 `.tmp` 再 `os.replace()` —— 与 `core/credentials.py` 同一条纪律。
        直接覆盖写在断电/崩溃时会留下**半个 JSON**，而"续跑时读到半个文件"
        比"文件不存在"难查得多。

【★ 为什么断点里必须存 node_outputs】
        续跑的关键不是"知道跑到哪了"，而是"**已完成节点的产出还在**" ——
        否则续跑会从空产出开始，下游拿到 `None`，表现为"莫名其妙的数据错误"。
        这正是本项目 L4 那次静默失效的同类风险：**状态没存全，问题在远处爆发**。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from core.orchestration.model import Checkpoint, Run, Step, StepState


def runs_dir() -> Path:
    """运行态目录（惰性取 `app_data_dir()`，避免 import 期就触碰配置）。"""
    from config import app_data_dir
    d = app_data_dir() / "workflows" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


#: run_id 只允许这些字符 —— 它会**直接拼进文件路径**，必须挡住路径穿越。
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def _safe_run_id(run_id: str) -> str:
    rid = str(run_id or "").strip()
    if not rid or not _RUN_ID_RE.match(rid) or ".." in rid:
        raise ValueError(
            f"非法的 run_id {run_id!r}：只允许字母/数字/._:- 且不含 '..'")
    return rid


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子写 JSON：临时文件 + `os.replace`。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp",
                                    prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # 失败要清掉临时文件，别留一地 .tmp
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _run_to_dict(run: Run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "state": run.state.value,
        # ★ `inputs` 也必须过 `_jsonable` —— 引擎会把**运行时对象**（如知识库实例
        # `__kb__`）注入 inputs，它们不可 JSON 序列化。第一版这里直接塞原值，
        # 结果落盘抛 TypeError 把整个运行带崩（守卫实测抓到）。
        "inputs": {k: _jsonable(v) for k, v in (run.inputs or {}).items()},
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "steps": {
            nid: {
                "node_id": s.node_id,
                "state": s.state.value,
                "attempts": s.attempts,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "error": s.error,
                "rejects": s.rejects,
                # output 可能不是 JSON 可序列化的 → 降级成 str，绝不因为存不下来就丢运行
                "output": _jsonable(s.output),
            }
            for nid, s in run.steps.items()
        },
    }


def _jsonable(v: Any) -> Any:
    """尽量保留结构；不可序列化时降级为字符串（**不抛异常**）。"""
    try:
        json.dumps(v, ensure_ascii=False)
        return v
    except (TypeError, ValueError):
        return str(v)


def save_run(run: Run) -> Path:
    """落盘一次运行态。"""
    run.updated_at = datetime.now().isoformat(timespec="seconds")
    path = runs_dir() / f"{_safe_run_id(run.run_id)}.json"
    _atomic_write_json(path, _run_to_dict(run))
    return path


def load_run(run_id: str) -> Run | None:
    """读回一次运行态。不存在返回 `None`（**不抛异常** —— 调用方要能区分"没有"与"读坏了"）。"""
    path = runs_dir() / f"{_safe_run_id(run_id)}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    steps: dict[str, Step] = {}
    for nid, s in (raw.get("steps") or {}).items():
        try:
            state = StepState(s.get("state", "pending"))
        except ValueError:
            state = StepState.PENDING
        steps[nid] = Step(
            node_id=s.get("node_id", nid),
            state=state,
            attempts=int(s.get("attempts") or 0),
            started_at=str(s.get("started_at") or ""),
            ended_at=str(s.get("ended_at") or ""),
            error=str(s.get("error") or ""),
            output=s.get("output"),
            rejects=int(s.get("rejects") or 0),
        )
    try:
        state = StepState(raw.get("state", "running"))
    except ValueError:
        state = StepState.RUNNING
    return Run(
        run_id=raw.get("run_id", run_id),
        workflow_id=raw.get("workflow_id", ""),
        state=state,
        steps=steps,
        inputs=raw.get("inputs") or {},
        started_at=raw.get("started_at", ""),
        updated_at=raw.get("updated_at", ""),
    )


def list_runs(workflow_id: str = "") -> list[dict[str, Any]]:
    """列出运行记录（按更新时间的文件名倒序即近似时间倒序）。

    只返回**摘要**，不解析完整 steps —— 列表页不需要全部内容。
    """
    out: list[dict[str, Any]] = []
    for p in sorted(runs_dir().glob("*.json"), reverse=True):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if workflow_id and raw.get("workflow_id") != workflow_id:
            continue
        out.append({
            "run_id": raw.get("run_id", p.stem),
            "workflow_id": raw.get("workflow_id", ""),
            "state": raw.get("state", ""),
            "started_at": raw.get("started_at", ""),
            "updated_at": raw.get("updated_at", ""),
            "step_count": len(raw.get("steps") or {}),
        })
    return out


# ---------------------------------------------------------------------------
# Checkpoint（续跑用）
# ---------------------------------------------------------------------------


def save_checkpoint(cp: Checkpoint) -> Path:
    """落盘断点。**`node_outputs` 必须完整** —— 缺了它续跑就会从空产出开始。"""
    path = runs_dir() / f"{_safe_run_id(cp.run_id)}.ckpt.json"
    payload = {
        "run_id": cp.run_id,
        "workflow_id": cp.workflow_id,
        "node_outputs": {k: _jsonable(v) for k, v in cp.node_outputs.items()},
        "node_states": dict(cp.node_states),
        "waiting_node": cp.waiting_node,
        "created_at": cp.created_at or datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_json(path, payload)
    return path


def load_checkpoint(run_id: str) -> Checkpoint | None:
    path = runs_dir() / f"{_safe_run_id(run_id)}.ckpt.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return Checkpoint(
        run_id=raw.get("run_id", run_id),
        workflow_id=raw.get("workflow_id", ""),
        node_outputs=raw.get("node_outputs") or {},
        node_states=raw.get("node_states") or {},
        waiting_node=raw.get("waiting_node", ""),
        created_at=raw.get("created_at", ""),
    )


def new_run_id(workflow_id: str = "") -> str:
    """生成新 run_id（时间戳 + 短随机后缀，避免同秒碰撞）。"""
    import secrets
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    prefix = re.sub(r"[^A-Za-z0-9_-]", "-", workflow_id)[:32] or "wf"
    return f"{prefix}-{stamp}-{suffix}"


__all__ = [
    "list_runs", "load_checkpoint", "load_run", "new_run_id",
    "runs_dir", "save_checkpoint", "save_run",
]