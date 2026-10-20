# -*- coding: utf-8 -*-
"""编排层的**纯数据模型** —— 只放数据结构与状态机，不依赖任何执行逻辑。

【为什么单独一个模块】项目在 `core/tools/` 抽取时踩过一次循环导入
        （`plot` 需要 `ToolResult`，最后靠拆出 `result.py` 解决）。
        编排层依赖更复杂，所以**先把 model 拆干净**：
        它只依赖标准库，`loader` / `validator` / `engine` 全都单向依赖它。

【P0 范围】本模块只定义**定义态**（Workflow / Node / Edge）与**运行态**（Run / Step）的结构，
        以及 §10 的**7 态状态机**约束。执行器在 `engine.py`（P1）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# 状态机（`docs/ORCHESTRATION.md` §10：7 态 + 单调性）
# ---------------------------------------------------------------------------


class StepState(str, Enum):
    """一个节点（step）的运行状态。"""

    PENDING = "pending"          # 已定义，未开始
    RUNNING = "running"          # 执行中（可有重试）
    DONE = "done"                # 成功
    WAITING = "waiting"          # 等待人工确认
    RESUMED = "resumed"          # 人工放行后回到 running
    BLOCKED = "blocked"          # 熔断（打回次数超限）—— **终态，不回退到别的分支**
    FAILED = "failed"            # 不可重试的失败 —— 终态


#: 终态：一旦进入就不能再变（**单调性**）。`WAITING` / `RESUMED` 是中间态，可以继续走。
TERMINAL_STATES: frozenset[StepState] = frozenset({
    StepState.DONE, StepState.BLOCKED, StepState.FAILED,
})

#: 合法迁移表。`docs/ORCHESTRATION.md` §10 的箭头逐条落成数据。
#: ★ 刻意**没有** `BLOCKED -> 其它` 与 `FAILED -> 其它`：熔断与不可重试失败都是**终态**。
ALLOWED_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset({StepState.RUNNING, StepState.WAITING,
                                  StepState.BLOCKED, StepState.FAILED}),
    StepState.RUNNING: frozenset({StepState.DONE, StepState.WAITING,
                                  StepState.BLOCKED, StepState.FAILED}),
    StepState.WAITING: frozenset({StepState.RESUMED, StepState.BLOCKED}),
    StepState.RESUMED: frozenset({StepState.RUNNING}),
    StepState.DONE: frozenset(),        # 终态
    StepState.BLOCKED: frozenset(),     # 终态（熔断）
    StepState.FAILED: frozenset(),      # 终态
}


class TransitionError(ValueError):
    """非法状态迁移（含"从终态出发"）。"""


def assert_transition(src: StepState, dst: StepState) -> None:
    """校验一次状态迁移；不合法时抛 `TransitionError`。

    ★ **终态不可出**是这个函数最重要的职责 —— 它挡住的就是
    "一个已经熔断的节点，被别的分支又拉起来跑"这类倒挂。
    """
    if src == dst:
        return
    if dst not in ALLOWED_TRANSITIONS.get(src, frozenset()):
        raise TransitionError(f"非法状态迁移: {src.value} → {dst.value}")


# ---------------------------------------------------------------------------
# 副作用声明（`docs/ORCHESTRATION.md` §5.3）
# ---------------------------------------------------------------------------


class SideEffect(str, Enum):
    """节点对**外部世界**的影响程度。决定"能不能重试""要不要人确认"。"""

    NONE = "none"                # 纯计算 / 只读
    DISK = "disk"                # 写盘（如图表落盘）
    NETWORK = "network"          # 调外部接口


#: ★ **默认 = DISK**：没声明副作用的节点按"会写盘"对待 ——
#: 与 `core/tools/capabilities.py` 的"缺省即保守"同一条纪律：
#: **忘记声明的后果应该是更严格，而不是更宽松。**
DEFAULT_SIDE_EFFECT = SideEffect.DISK

#: 只有 `NONE` 的节点默认可重试（`docs/ORCHESTRATION.md` §5.3）。
RETRIABLE_SIDE_EFFECTS: frozenset[SideEffect] = frozenset({SideEffect.NONE})


# ---------------------------------------------------------------------------
# 定义态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """一个输入参数的声明。"""

    name: str
    type: str = "string"                     # string / number / boolean / object / array
    required: bool = False
    default: Any = None
    description: str = ""


@dataclass
class Node:
    """工作流里的一个节点（定义态）。

    `type` 形如 `tool.get_weather` / `data.kb_query` / `llm.decide` / `gate.numeric_consistency`
    / `human.confirm`。**是否合法由 `catalog.py` 判定**，本类不做语义校验。
    """

    id: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    #: 显式副作用声明。为 `None` 表示**未声明** → 按 `DEFAULT_SIDE_EFFECT` 保守处理。
    side_effect: SideEffect | None = None
    #: 依赖的上游节点 id（边的起点）。空表示"无依赖、开局即可跑"。
    needs: list[str] = field(default_factory=list)
    #: 节点级重试次数（默认 0）。★ 非幂等节点即使写了也会被取 0（见 `effective_retry`）。
    retry: int = 0
    timeout_s: float | None = None
    #: 失败策略：`fail_fast` / `degrade` / `skip_if_optional`
    on_fail: str = "fail_fast"
    #: 是否可选（配合 `skip_if_optional`）
    optional: bool = False
    description: str = ""

    # ---- 派生属性 ----
    @property
    def effective_side_effect(self) -> SideEffect:
        """实际生效的副作用（未声明 → 保守默认）。"""
        return self.side_effect if self.side_effect is not None else DEFAULT_SIDE_EFFECT

    @property
    def effective_retry(self) -> int:
        """实际生效的重试次数。

        ★ **非幂等节点一律 0** —— 即使有人手写了 `retry: 3`。
        重复写盘比"少重试一次"危险得多。
        """
        if self.effective_side_effect not in RETRIABLE_SIDE_EFFECTS:
            return 0
        return max(0, int(self.retry or 0))


@dataclass
class Workflow:
    """一份工作流定义（`workflows/*.yaml` 的模型）。"""

    id: str
    version: int = 1
    nodes: list[Node] = field(default_factory=list)
    #: 输入声明：名字 → ParamSpec
    inputs: dict[str, ParamSpec] = field(default_factory=dict)
    description: str = ""
    #: 源文件（便于错误定位）
    source: str = ""

    # ---- 查询辅助 ----
    def node_map(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def node(self, node_id: str) -> Node | None:
        return self.node_map().get(node_id)

    def edges(self) -> list[tuple[str, str]]:
        """把 `needs` 摊平成边列表 `(上游, 下游)`。"""
        out: list[tuple[str, str]] = []
        for n in self.nodes:
            for dep in n.needs:
                out.append((dep, n.id))
        return out

    def ids(self) -> list[str]:
        return [n.id for n in self.nodes]


# ---------------------------------------------------------------------------
# 运行态（P0 只定义结构；填充由 P1 的 engine 负责）
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """一个节点在**某次运行**中的状态。"""

    node_id: str
    state: StepState = StepState.PENDING
    attempts: int = 0
    started_at: str = ""
    ended_at: str = ""
    error: str = ""
    #: 节点产出（供下游引用 `{{node.field}}`）
    output: Any = None
    #: 人工打回计数（`human.confirm` 用；达到 `max_reject` 即熔断）
    rejects: int = 0

    def transition(self, dst: StepState) -> None:
        """走一步状态机。非法迁移抛 `TransitionError`。"""
        assert_transition(self.state, dst)
        self.state = dst


@dataclass
class Run:
    """一次工作流运行。"""

    run_id: str
    workflow_id: str
    state: StepState = StepState.RUNNING
    steps: dict[str, Step] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    updated_at: str = ""

    def step(self, node_id: str) -> Step:
        """取（必要时创建）某节点的运行态。"""
        s = self.steps.get(node_id)
        if s is None:
            s = Step(node_id=node_id)
            self.steps[node_id] = s
        return s

    def all_terminal(self) -> bool:
        return bool(self.steps) and all(s.state in TERMINAL_STATES for s in self.steps.values())


@dataclass
class Checkpoint:
    """可续跑的断点快照（`store.py` 落盘用）。

    `node_outputs` 是关键：续跑时**已完成节点直接从它取产出，不重新执行**。
    """

    run_id: str
    workflow_id: str
    node_outputs: dict[str, Any] = field(default_factory=dict)
    node_states: dict[str, str] = field(default_factory=dict)
    waiting_node: str = ""
    created_at: str = ""


__all__ = [
    "ALLOWED_TRANSITIONS", "DEFAULT_SIDE_EFFECT", "RETRIABLE_SIDE_EFFECTS",
    "TERMINAL_STATES", "Checkpoint", "Node", "ParamSpec", "Run", "SideEffect",
    "Step", "StepState", "TransitionError", "Workflow", "assert_transition",
]