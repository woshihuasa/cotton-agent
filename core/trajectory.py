# -*- coding: utf-8 -*-
"""轨迹录制与导出 —— 把真实问答过程变成**可被下游复用**的数据。

【为什么分两层】**原始轨迹**（本模块的 `Recorder`）和**导出格式**（`to_sharegpt`）分开：

    · 原始轨迹是**单一真相**：全保真（含工具调用、推理、用量、路由），一次录制、多种导出；
    · 导出是**纯函数派生**：格式改了只重导，**不需要重跑问答**（重跑要花钱）。

    ★ 这正是本项目一直在防的「两份地图」：**只存一份真数据，格式是视图**。

【开关】**`TRAJECTORY_PATH` 为空 = 关闭**（默认）。
    比单独一个布尔开关省心：**路径本身就是配置**，不会出现"开了但不知道写去哪"的状态。
    关闭时 `ask()` 里只多一次 `enabled()` 判断，**行为与改动前逐字一致**。

【ShareGPT 约定】（对齐 hermes-agent 的轨迹格式，MIT 可抄）
    · **推理统一归一为 `<think>…</think>`**，无推理时补空块 ——
      保证 HF 数据集**每一行的列结构一致**（不然训练脚本要处理两种形态）；
    · **工具调用包 XML**：`<tool_call>{…}</tool_call>`、`<tool_response>{…}</tool_response>`；
    · **`tool_stats` 恒含全部工具名**（值为 0 也写）—— 同上，为列一致。
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: 原始轨迹的 schema 版本。**改格式必须升版本**，否则下游无法区分。
SCHEMA_VERSION = "cotton-trajectory/v1"

#: ShareGPT 里工具轮的 `from` 值（社区通行做法）
FROM_HUMAN = "human"
FROM_GPT = "gpt"
FROM_TOOL = "tool"


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


def trajectory_path() -> str:
    """轨迹 JSONL 的目标路径。**空串 = 未开启**。"""
    try:
        import config
        return str(getattr(config, "TRAJECTORY_PATH", "") or "").strip()
    except Exception:                                        # noqa: BLE001
        return os.getenv("TRAJECTORY_PATH", "").strip()


def enabled() -> bool:
    return bool(trajectory_path())


# ---------------------------------------------------------------------------
# 当前记录器（contextvar）
# ---------------------------------------------------------------------------

#: ★ 用 **contextvar** 而不是改 `_execute_tool_loop` 的签名 ——
#: 与 `tool_caps.set_mode()` 同一套做法：那个函数被 `tests/eval_numeric.py`
#: 以**未绑定静态方法**方式调用过，改签名会打破既有测试。
_CURRENT: "ContextVar[Recorder | None]" = ContextVar("trajectory_recorder", default=None)


def current() -> "Recorder | None":
    """取当前上下文里的记录器（未开启录制时是 `None`）。"""
    return _CURRENT.get()


def set_current(recorder: "Recorder | None"):
    """设置 / 清除当前记录器。返回 contextvar token（供 finally 里 reset）。"""
    return _CURRENT.set(recorder)


# ---------------------------------------------------------------------------
# 原始轨迹
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """一次工具调用（含结果）。"""

    name: str
    arguments: str = ""          #: 模型原始发出的 arguments 字符串
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    result: str = ""
    call_id: str = ""


@dataclass
class Round:
    """工具循环里的一轮。"""

    index: int
    content: str = ""            #: assistant 的正文（通常为空，模型只在调工具）
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Recorder:
    """一次问答的轨迹记录器。由 `RAGEngine.ask()` 创建、`finish()` 落盘。"""

    question: str
    model: str = ""
    provider: str = ""
    system_prompt: str = ""
    route: str = ""
    rounds: list[Round] = field(default_factory=list)
    answer: str = ""
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # ---- 录制 ----
    def start_round(self, content: str = "", reasoning: str = "") -> Round:
        r = Round(index=len(self.rounds) + 1, content=content, reasoning=reasoning)
        self.rounds.append(r)
        return r

    def add_tool_call(self, rnd: Round, *, name: str, arguments: str,
                      args: dict[str, Any] | None = None, call_id: str = "") -> ToolCall:
        tc = ToolCall(name=name, arguments=arguments,
                      args=dict(args or {}), call_id=call_id)
        rnd.tool_calls.append(tc)
        return tc

    def finish(self, answer: str, reasoning: str = "",
               usage: dict[str, Any] | None = None) -> dict[str, Any]:
        """收尾并返回原始记录 dict（不落盘，落盘由调用方决定）。"""
        self.answer = answer or ""
        self.reasoning = reasoning or ""
        self.usage = dict(usage or {})
        return self.to_record()

    # ---- 序列化 ----
    def to_record(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "created_at": self.created_at,
            "model": self.model,
            "provider": self.provider,
            "route": self.route,
            "system_prompt": self.system_prompt,
            "question": self.question,
            "rounds": [
                {
                    "index": r.index,
                    "content": r.content,
                    "reasoning": r.reasoning,
                    "tool_calls": [
                        {"name": t.name, "arguments": t.arguments,
                         "args": t.args, "ok": t.ok,
                         "result": t.result, "call_id": t.call_id}
                        for t in r.tool_calls
                    ],
                }
                for r in self.rounds
            ],
            "answer": self.answer,
            "reasoning": self.reasoning,
            "usage": self.usage,
            "error": self.error,
        }


def append_record(record: dict[str, Any], path: str = "") -> Path | None:
    """把一条原始轨迹追加到 JSONL。

    **失败不抛异常、不影响问答** —— 录制是旁路，绝不能因为写不进去就搞砸回答。
    返回写入的路径；跳过时返回 `None`。
    """
    target = (path or trajectory_path()).strip()
    if not target:
        return None
    try:
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return p
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 导出：原始 → ShareGPT
# ---------------------------------------------------------------------------

#: 已知工具名（导出时用来把 `tool_stats` 补齐成**固定列**）。
#: 导入失败时退回空元组 —— 导出仍可用，只是 stats 列不保证齐。
def _all_tool_names() -> list[str]:
    try:
        from core.tools import TOOL_HANDLERS
        return sorted(TOOL_HANDLERS)
    except Exception:                                        # noqa: BLE001
        return []


def _wrap_think(text: str, *, include_empty: bool) -> str:
    """把推理内容包成 `<think>…</think>`。

    `include_empty=True` 时**无推理也补空块** —— 这是为了让 HF 数据集的每一行
    结构一致（不然训练脚本要处理"有无 think 块"两种形态）。
    """
    body = (text or "").strip()
    if not body and not include_empty:
        return ""
    return f"<think>\n{body}\n</think>" if body else "<think>\n\n</think>"


def _tool_call_xml(call: dict[str, Any]) -> str:
    payload = {
        "name": call.get("name", ""),
        "arguments": call.get("args") or _maybe_json(call.get("arguments", "")),
    }
    return "<tool_call>\n" + json.dumps(payload, ensure_ascii=False) + "\n</tool_call>"


def _tool_response_xml(call: dict[str, Any]) -> str:
    payload = {"name": call.get("name", ""), "ok": bool(call.get("ok", True)),
               "content": str(call.get("result", ""))}
    return "<tool_response>\n" + json.dumps(payload, ensure_ascii=False) + "\n</tool_response>"


def _maybe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (ValueError, TypeError):
        return str(raw)


def to_sharegpt(record: dict[str, Any], *, include_empty_think: bool = True,
                tool_names: list[str] | None = None) -> dict[str, Any]:
    """把一条原始轨迹转成 ShareGPT 行（**纯函数**，同一条可反复导出）。

    输出**列结构固定**，可直接喂 `datasets.load_dataset("json", ...)`：

        id / system / conversations / tools / tool_stats / model / created_at

    `tool_stats` **恒含全部工具名**（值为 0 也写）——
    否则 HF 数据集会把不同行当成不同 schema。
    """
    names = list(tool_names) if tool_names is not None else _all_tool_names()
    stats: dict[str, int] = {n: 0 for n in names}

    convs: list[dict[str, str]] = [
        {"from": FROM_HUMAN, "value": str(record.get("question") or "")}
    ]

    rounds = record.get("rounds") or []
    for rnd in rounds:
        calls = rnd.get("tool_calls") or []
        if not calls:
            continue
        # ① assistant 侧：正文（通常空）+ 推理 + 工具调用
        parts: list[str] = []
        think = _wrap_think(rnd.get("reasoning", ""), include_empty=include_empty_think)
        if think:
            parts.append(think)
        body = str(rnd.get("content") or "").strip()
        if body:
            parts.append(body)
        parts.extend(_tool_call_xml(c) for c in calls)
        convs.append({"from": FROM_GPT, "value": "\n\n".join(parts)})

        # ② 工具侧：每个结果一条
        for c in calls:
            nm = str(c.get("name") or "")
            stats[nm] = stats.get(nm, 0) + 1
            convs.append({"from": FROM_TOOL, "value": _tool_response_xml(c)})

    # ③ 最终回答（同样带 think 块，保持列一致）
    final_parts: list[str] = []
    think = _wrap_think(record.get("reasoning", ""), include_empty=include_empty_think)
    if think:
        final_parts.append(think)
    final_parts.append(str(record.get("answer") or ""))
    convs.append({"from": FROM_GPT, "value": "\n\n".join(final_parts)})

    return {
        "id": _make_id(record),
        "system": str(record.get("system_prompt") or ""),
        "conversations": convs,
        # 工具清单以**字符串**给出（HF 要求列类型稳定；嵌 list 会变 object）
        "tools": json.dumps(names, ensure_ascii=False),
        "tool_stats": stats,
        "model": str(record.get("model") or ""),
        "created_at": str(record.get("created_at") or ""),
    }


def _make_id(record: dict[str, Any]) -> str:
    import hashlib
    raw = json.dumps([record.get("created_at"), record.get("question"),
                      record.get("answer")], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """读原始轨迹 JSONL。**坏行跳过而不是整批失败**（一份文件里混着 v1/v2 也要能读）。"""
    p = Path(path)
    out: list[dict[str, Any]] = []
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("schema") == SCHEMA_VERSION:
            out.append(rec)
    return out


def export_file(src: str | Path, dst: str | Path, *,
                include_empty_think: bool = True) -> dict[str, int]:
    """把原始轨迹文件导出为 ShareGPT JSONL。

    返回统计：`{"read": n, "written": m, "skipped": k}`。
    **跳过空回答**（`answer` 为空 = 那一轮没产出，喂给训练只会教模型别说话）。
    """
    records = read_records(src)
    rows: list[dict[str, Any]] = []
    skipped = 0
    for rec in records:
        if not str(rec.get("answer") or "").strip():
            skipped += 1
            continue
        rows.append(to_sharegpt(rec, include_empty_think=include_empty_think))

    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"read": len(records), "written": len(rows), "skipped": skipped}


__all__ = [
    "FROM_GPT", "FROM_HUMAN", "FROM_TOOL", "SCHEMA_VERSION", "Recorder",
    "Round", "ToolCall", "append_record", "current", "enabled", "export_file",
    "read_records", "set_current", "to_sharegpt", "trajectory_path",
]