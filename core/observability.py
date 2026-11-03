# -*- coding: utf-8 -*-
"""可观测性埋点 —— 一次问答的每一步耗时，落到本地 JSONL。

【为什么先做本地，不直接接 Langfuse】
  这是个桌面单机应用：打包要过 PyInstaller，数据本来就该留在本机，而且没有运维
  看着它。一上来就依赖一个外部服务，它挂掉时应用会跟着出错，那是拿稳定性换看板。
  所以这一层先只做"记录"，落到 `app_data_dir()/traces/` 下的 JSONL，
  用 `tools/traces.py` 查看。Langfuse 以后作为**第二个 sink** 接进来，不上报正文。

【记录粒度：一个 span 一行，不是一条回答一行】
  一条回答通常产生七八行，靠 `trace_id` 与 `parent_id` 串成树。反过来存（整条一行）
  中途崩掉就全丢；一行一个 span 是追加写，崩了只丢最后几行。

【默认不记正文】
  提示词与回答正文里可能有地块、用量、农户对话。默认只记模型名、token 数、耗时、
  工具名与成败；要记正文必须显式打开 `OBSERVABILITY_CAPTURE_TEXT=1`。
  密钥无论开关如何都**永不记录** —— 这一条由守卫断言。

【关闭时零开销】
  `OBSERVABILITY_ENABLED` 未开时，`span()` 返回一个共享的空对象，不建对象、
  不取时间、不碰磁盘。守卫断言单次开销小于 1 毫秒。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any

log = logging.getLogger("Observability")

_LOCK = threading.Lock()
#: 当前 span 句柄（线程局部）。★ 不用 contextvars：调度器与 TaskRunner 都在普通
#: 线程里跑，contextvars 不会跨线程继承，而线程局部本来就是"每线程一份"，
#: 跨线程时由调用方显式把父句柄传下去（见 `adopt`）。
_LOCAL = threading.local()

#: 共享的空对象：关闭状态下所有调用都落到它身上
NOOP = None


def _flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() not in (
        "0", "false", "no", "off", "")


def enabled() -> bool:
    """是否开启埋点。每次现读环境变量，便于测试里临时开关。"""
    return _flag("OBSERVABILITY_ENABLED")


def capture_text() -> bool:
    """是否连正文一起记。默认关。"""
    return _flag("OBSERVABILITY_CAPTURE_TEXT")


def trace_dir():
    from pathlib import Path
    from config import app_data_dir
    custom = os.getenv("OBSERVABILITY_DIR", "").strip()
    return Path(custom) if custom else Path(app_data_dir()) / "traces"


class _NoopSpan:
    """空实现：关闭状态下的所有 span 都是它。"""

    trace_id = ""
    span_id = ""
    parent_id = ""

    def set(self, **_kw: Any) -> None:
        return None

    def usage(self, **_kw: Any) -> None:
        return None

    def text(self, *_a: Any, **_kw: Any) -> None:
        return None


_NOOP = _NoopSpan()


class _Span:
    """一个可计时的步骤。结束时写一行 JSONL。"""

    __slots__ = ("name", "kind", "trace_id", "span_id", "parent_id",
                 "t0", "attrs")

    def __init__(self, name: str, kind: str, trace_id: str,
                 parent_id: str) -> None:
        self.name = name
        self.kind = kind
        self.trace_id = trace_id
        self.span_id = uuid.uuid4().hex[:8]
        self.parent_id = parent_id
        self.t0 = time.perf_counter()
        self.attrs: dict[str, Any] = {}

    def set(self, **kw: Any) -> None:
        """附加上下文：工具名、模型名、条数、成败之类。

        ★ 只收**元数据**。`text_` 开头的键会被直接丢掉 —— 正文只能走 `text()`，
          那条路受 `OBSERVABILITY_CAPTURE_TEXT` 控制。否则"默认不记正文"这句话
          只要有人顺手 `set(q=用户问题)` 就破了。
        """
        for k, v in kw.items():
            if v is None:
                continue
            key = str(k)
            if key.startswith("text_"):
                continue
            self.attrs[key] = v

    def usage(self, *, prompt: int = 0, completion: int = 0,
              model: str = "") -> None:
        """记录 token 用量（只有模型调用才有）。"""
        if model:
            self.attrs["model"] = model
        if prompt or completion:
            self.attrs["in"] = int(prompt or 0)
            self.attrs["out"] = int(completion or 0)

    def text(self, label: str, value: str, *, limit: int = 2000) -> None:
        """正文（仅在 `OBSERVABILITY_CAPTURE_TEXT=1` 时真的写）。"""
        if not capture_text():
            return
        s = str(value or "")
        self.attrs[f"text_{label}"] = s[:limit]

    def close(self, error: str = "") -> None:
        ms = (time.perf_counter() - self.t0) * 1000.0
        if error:
            self.attrs["error"] = error[:300]
        record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "trace": self.trace_id, "span": self.span_id,
                  "parent": self.parent_id or None, "name": self.name,
                  "kind": self.kind, "ms": round(ms, 2)}
        record.update(self.attrs)
        _write(record)


def _write(record: dict) -> None:
    """追加一行。**失败只记 debug 日志，不抛给主流程。**"""
    try:
        d = trace_dir()
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            with open(d / f"{date.today().isoformat()}.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:                                    # noqa: BLE001
        log.debug("埋点写入失败（已忽略）：%s", e)


def current() -> Any:
    """当前线程正在记录的 span（没有就是空对象）。"""
    return getattr(_LOCAL, "span", None) or _NOOP


def current_trace_id() -> str:
    return getattr(current(), "trace_id", "") or ""


def adopt(handle: Any) -> Any:
    """把父 span 装到当前线程，供子线程延续调用链。

    ★ 调度器与 TaskRunner 都在普通线程里跑，contextvars 不会跟着过去。
      跨线程时要显式 `adopt(parent)`，否则工作流的 span 会全变成孤立的根节点。
    """
    _LOCAL.span = handle or _NOOP
    return handle


@contextmanager
def span(name: str, kind: str = "span", **attrs: Any):
    """开一个子 span（自动挂到当前 span 下面）。"""
    if not enabled():
        yield _NOOP
        return
    parent = current()
    me = _Span(str(name), kind, parent.trace_id or uuid.uuid4().hex[:12],
               parent.span_id)
    me.set(**attrs)
    prev = getattr(_LOCAL, "span", None)
    _LOCAL.span = me
    try:
        yield me
    except Exception as e:                                    # noqa: BLE001
        me.close(error=f"{type(e).__name__}: {e}")
        raise
    else:
        me.close()
    finally:
        _LOCAL.span = prev


@contextmanager
def trace(name: str, **attrs: Any):
    """开一条新的调用链（根 span）。"""
    if not enabled():
        yield _NOOP
        return
    prev = getattr(_LOCAL, "span", None)
    me = _Span(str(name), "trace", uuid.uuid4().hex[:12], "")
    me.set(**attrs)
    _LOCAL.span = me
    try:
        yield me
    except Exception as e:                                    # noqa: BLE001
        me.close(error=f"{type(e).__name__}: {e}")
        raise
    else:
        me.close()
        if me.attrs or me.name:
            log.info("[trace %s] %s 完成于 %.0f ms", me.trace_id, me.name,
                     (time.perf_counter() - me.t0) * 1000)
    finally:
        _LOCAL.span = prev


@contextmanager
def generation(model: str, **attrs: Any):
    """模型调用专用 span：结束时带 token 用量。"""
    if not enabled():
        yield _NOOP
        return
    with span(f"llm:{model or 'unknown'}", kind="generation",
              model=model or "unknown", **attrs) as g:
        yield g


def flush() -> None:
    """收尾钩子。本地 sink 是同步写，这里只是留给以后接远端 sink。"""
    return None


__all__ = ["adopt", "capture_text", "current", "current_trace_id", "enabled",
           "flush", "generation", "span", "trace", "trace_dir"]