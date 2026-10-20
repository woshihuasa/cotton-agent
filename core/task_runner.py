# -*- coding: utf-8 -*-
"""任务执行器 —— 在**后台线程**跑编排层工作流，不阻塞对话。

【为什么需要它（`docs/PERCEPTION.md` §五）】

    两件事都缺同一块：
      ① **移交**（`core/handoff.py`）—— 判定与打包做好了，但**从没执行过**；
      ② **感知任务** —— 本身就是一次工作流执行。

    而两者都**不能**塞进 `ask()`：
      `ask()` 是流式生成器，边跑边 `yield`。若在里面同步跑一个几分钟的工作流，
      界面会一直转圈、一句反馈都没有。**且两者都在 `ask()` 里 = 它会变成
      "什么都干的长事务"。**

    ★ 所以：`ask()` 只**产出意图并立即结束**，真正执行交给本模块。
      一份工作，两个入口共用。

【设计要点】

    · **框架无关**：用回调 `on_event`，不依赖 PyQt。
      UI 侧接成信号，测试侧传个 list.append 即可。
    · **失败不静默**：任何异常都转成 `failed` 事件并带上错误文本。
    · **可取消**：`cancel()` 请求取消；已跑的节点由编排层的断点保住，可续跑。
    · **静态校验先行**：YAML 不合格**不启动线程** —— 不留一个"看起来在跑"的空任务。

【它不做的事】
    · 不重试（重试策略留给调用方）
    · 不持久化队列（进程退出即丢；断点由编排层的 `store` 负责）
    · 不碰 UI
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

#: 事件类型（字符串常量，便于跨线程/落盘）
EV_STARTED = "started"
EV_PROGRESS = "progress"
EV_FINISHED = "finished"
EV_FAILED = "failed"
EV_CANCELLED = "cancelled"

ALL_EVENTS = (EV_STARTED, EV_PROGRESS, EV_FINISHED, EV_FAILED, EV_CANCELLED)


# ---------------------------------------------------------------------------
# 校验（单一实现 —— `core/handoff.py` 也调这里，避免两份地图）
# ---------------------------------------------------------------------------


def load_and_validate(yaml_text: str) -> tuple[Any, list[str]]:
    """解析 + 静态校验。返回 `(workflow, 问题列表)`。

    **问题列表非空即视为不合格**（Fail-Closed）。

    ★ 为什么这一步在任何执行之前：LLM 生成的流程可能参数错、依赖成环、
      引用不存在的节点 —— `validator` 里 10 类错误各有反例，它就是为这一刻准备的。
    """
    from core.orchestration import loader, validator

    try:
        wf = loader.load_text(yaml_text)
    except Exception as e:                                   # noqa: BLE001
        return None, [f"YAML 解析失败：{type(e).__name__}: {e}"]

    try:
        issues = validator.validate(wf)
    except Exception as e:                                   # noqa: BLE001
        return None, [f"静态校验异常：{type(e).__name__}: {e}"]

    msgs: list[str] = []
    for i in issues:
        try:
            m = validator.format_issues([i])
        except Exception:                                    # noqa: BLE001
            m = ""
        msgs.append(m or str(i))
    return wf, msgs


# ---------------------------------------------------------------------------
# 事件与句柄
# ---------------------------------------------------------------------------


@dataclass
class TaskEvent:
    """一次任务事件。**纯数据**，可直接跨线程/喂给 UI。"""

    kind: str
    run_id: str
    workflow_id: str = ""
    #: 进度用：当前节点 / 已完成数 / 总数（编排层 `on_event` 给什么就带什么）
    payload: dict = field(default_factory=dict)
    #: 失败时的错误文本（`failed` 事件必有）
    error: str = ""
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "run_id": self.run_id,
                "workflow_id": self.workflow_id, "payload": self.payload,
                "error": self.error, "ts": self.ts}


@dataclass
class TaskHandle:
    """一个已提交任务的句柄。"""

    run_id: str
    workflow_id: str
    #: 提交时校验产生的问题（非空 = **没有启动**）
    issues: list[str] = field(default_factory=list)
    #: 是否真的启动了一个线程
    launched: bool = False

    @property
    def ok(self) -> bool:
        return self.launched and not self.issues


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


class TaskRunner:
    """在后台线程跑工作流。

    用法：
        runner = TaskRunner(on_event=print)
        h = runner.submit_yaml(text, {"city": "阿克苏"})
        if not h.ok: ... 处理不合格 ...
        run = runner.wait(h.run_id, timeout=60)
    """

    def __init__(self, *, on_event: Callable[[TaskEvent], None] | None = None,
                 max_workers: int = 4) -> None:
        self._on_event = on_event
        self._max_workers = max_workers
        self._lock = threading.Lock()
        #: run_id → Run（成功/失败/取消后的终态）
        self._runs: dict[str, Any] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()
        self._errors: dict[str, str] = {}
        self._done = threading.Condition(self._lock)

    # -- 事件 --------------------------------------------------------

    def _emit(self, ev: TaskEvent) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(ev)
        except Exception:                                    # noqa: BLE001
            # ★ 回调抛异常**绝不能**弄死执行线程 —— 那会让任务静默消失。
            #   只记录，不传播（与"失败不静默"不冲突：任务本身的状态仍会更新）。
            traceback.print_exc()

    # -- 提交 --------------------------------------------------------

    def submit_yaml(self, yaml_text: str, inputs: dict | None = None, *,
                    run_id: str = "", workflow_id: str = "") -> TaskHandle:
        """校验后提交一段 YAML。**校验不过不启动线程。**"""
        wf, issues = load_and_validate(yaml_text)
        wid = workflow_id or getattr(wf, "id", "") or ""
        if wf is None or issues:
            h = TaskHandle(run_id=run_id or "(未启动)", workflow_id=wid,
                           issues=issues, launched=False)
            self._emit(TaskEvent(EV_FAILED, h.run_id, wid,
                                 error="工作流未通过静态校验：" + "；".join(issues[:3])))
            return h
        return self.submit_workflow(wf, inputs, run_id=run_id)

    def submit_workflow(self, wf: Any, inputs: dict | None = None, *,
                        run_id: str = "") -> TaskHandle:
        """提交一个**已解析**的 Workflow。"""
        from core.orchestration import engine as orch_engine
        from core.orchestration import store as orch_store

        wid = getattr(wf, "id", "") or ""
        if not run_id:
            run_id = orch_store.new_run_id(wid)

        h = TaskHandle(run_id=run_id, workflow_id=wid, launched=True)

        def _work() -> None:
            try:
                if run_id in self._cancelled:
                    self._emit(TaskEvent(EV_CANCELLED, run_id, wid))
                    return
                eng = orch_engine.Engine(
                    wf, max_workers=self._max_workers, persist=True,
                    on_event=lambda e: self._emit(
                        TaskEvent(EV_PROGRESS, run_id, wid,
                                  payload=e if isinstance(e, dict) else {"raw": e})))
                self._emit(TaskEvent(EV_STARTED, run_id, wid))
                run = eng.run(inputs or {}, run_id=run_id)
                with self._lock:
                    self._runs[run_id] = run
                # ★ 编排层用 StepState 表达终态；这里如实映射，不假装成功
                state = getattr(getattr(run, "state", None), "name", str(run))
                if run_id in self._cancelled:
                    self._emit(TaskEvent(EV_CANCELLED, run_id, wid,
                                         payload={"state": state}))
                elif state in ("DONE", "WAITING"):
                    # WAITING = 停在人工确认节点，不是失败
                    self._emit(TaskEvent(EV_FINISHED, run_id, wid,
                                         payload={"state": state}))
                else:
                    err = f"工作流结束状态为 {state}"
                    with self._lock:
                        self._errors[run_id] = err
                    self._emit(TaskEvent(EV_FAILED, run_id, wid,
                                         payload={"state": state}, error=err))
            except Exception as e:                           # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                with self._lock:
                    self._errors[run_id] = msg
                self._emit(TaskEvent(EV_FAILED, run_id, wid, error=msg))
                traceback.print_exc()
            finally:
                with self._done:
                    self._done.notify_all()

        t = threading.Thread(target=_work, name=f"task-{run_id}", daemon=True)
        with self._lock:
            self._threads[run_id] = t
        t.start()
        return h

    # -- 等待 / 取消 / 查询 ------------------------------------------

    def wait(self, run_id: str, timeout: float | None = None) -> Any:
        """等任务结束，返回 `Run`（失败/取消时为 `None`）。"""
        deadline = None if timeout is None else time.time() + timeout
        with self._done:
            while run_id not in self._runs and run_id not in self._errors:
                if run_id in self._cancelled and run_id not in self._threads:
                    return None
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    return None
                self._done.wait(remaining if remaining is not None else 0.2)
            return self._runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        """请求取消。

        ★ 只是**标记 + 让后续节点不再开始**；已经在跑的节点无法中断
          （Python 线程不可强杀）。已完成的节点由编排层断点保住，可续跑。
        """
        with self._lock:
            if run_id not in self._threads:
                return False
            self._cancelled.add(run_id)
        return True

    def error_of(self, run_id: str) -> str:
        with self._lock:
            return self._errors.get(run_id, "")

    def is_running(self, run_id: str) -> bool:
        t = self._threads.get(run_id)
        return bool(t and t.is_alive())

    def active(self) -> list[str]:
        """仍在跑的 run_id 列表。"""
        with self._lock:
            return [rid for rid, t in self._threads.items() if t.is_alive()]


__all__ = [
    "ALL_EVENTS", "EV_CANCELLED", "EV_FAILED", "EV_FINISHED", "EV_PROGRESS",
    "EV_STARTED", "TaskEvent", "TaskHandle", "TaskRunner", "load_and_validate",
]