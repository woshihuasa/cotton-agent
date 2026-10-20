# -*- coding: utf-8 -*-
"""节点目录 —— **哪些节点类型存在、各自的参数契约是什么**。

【核心设计：`tool.*` 节点从 `schemas.py` **派生**，不另抄一份】
        项目已经吃过"契约抄两份 → 必然漂移"的亏（`07` 号「两份地图必然对不上」）。
        所以这里**不写任何工具参数定义**，而是：
          `tool.get_weather` 的 schema ← `core.tools.schemas.TOOL_SCHEMAS` 里那份；
          `tool.plot_trend` 的副作用 ← `core.tools.capabilities.CAPABILITIES` 里那份。

        好处：工具改了参数，**编排层的节点契约自动跟着变**，且守卫
        `check_workflow_contracts.py` 会断言两者**逐字段一致**（漂移当场失败）。

【另一条：副作用从能力注解派生】
        工具层已经声明了 `read_only` / `destructive` / `open_world`，
        编排层的 `side_effect` 就是它们的**翻译**：
          · `read_only=True` 且 `open_world=False` → `none`（纯本地只读，可重试）
          · `read_only=True` 且 `open_world=True`  → `network`（调外部接口）
          · 其余（非只读/可破坏）                → `disk`（会改动外部世界）
        **不另造一套判据** —— 否则又会出现两处口径。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.orchestration.model import ParamSpec, SideEffect
from core.tools import TOOL_HANDLERS, TOOL_SCHEMAS
from core.tools import capabilities as tool_caps

#: 编排层支持的节点**族**。具体节点 = 族 + 名字（`tool.<name>`）。
NODE_FAMILIES: tuple[str, ...] = (
    "tool",       # 调用现有 6 个原生工具
    "data",       # 数据源：kb_query
    "llm",        # 带输出契约的模型节点
    "gate",       # 确定性门禁
    "human",      # 人工确认
    "perception",  # 感知计数（V1：不含 YOLO 推理）
    "report",      # 报告渲染
)

#: 首期（P0/P1）真正**可执行**的节点类型。
#: ⚠️ 刻意只列这些 —— `flow.sub` / `skill.*` / `notify.*` / `agent.*` 在文档里是 ⏳，
#: 文档已设计但**代码零实现**，所以这里**不谎报支持**。
EXECUTABLE_TYPES: frozenset[str] = frozenset({
    "data.kb_query",
    "llm.decide", "llm.summarize",
    "gate.numeric_consistency", "gate.range", "gate.mutual_exclusion",
    "gate.uncertainty",
    # ★ P5 的 ① 半边：让模型当"审查员"给上游产出打分并拦截。
    #   ⚠️ 它是**第一道过滤**，**不是**人类终审 —— 终审仍然只能由 `human.confirm` 表达。
    "gate.agent_review",
    "human.confirm",
    # ★ 感知与报告（`docs/PERCEPTION.md` V1）——
    #   ⚠️ 这两项与 `engine._EXECUTORS` 是**两份登记**，
    #   本文件是"静态校验用的白名单"，那份是"运行期用的分派表"。
    #   加节点必须两处都加，否则会出现"校验说不可执行、其实能跑"，
    #   或者反过来 —— 这正是本项目一直在防的「两份地图」。
    #   （`tests/check_perception.py` 断言两处一致。）
    "perception.record",
    "report.markdown",
    "report.partial",
})

#: 已设计但**当前不可执行**的节点类型 → 给出明确理由（而不是"未知类型"）。
#: ★ 这条区分很重要：**"还没做"和"拼错了"是两种错误**，报错信息必须能分开。
DESIGNED_NOT_IMPLEMENTED: dict[str, str] = {
    "flow.sub": "子工作流：随 engine 迭代（P1 之后）",
    "skill.*": "技能节点：编排引擎的可执行节点尚未接线（详见 docs/ABSORPTION.md §2.1 的欠账）",
    "notify.*": "外发推送：依赖「主动 Agent」，尚未实现",
    "agent.*": "子 Agent 委派：依赖「角色矩阵」（ROADMAP C2），尚未实现",
}


@dataclass(frozen=True)
class NodeSpec:
    """一个节点类型的**契约**。"""

    type: str
    params: tuple[ParamSpec, ...] = ()
    side_effect: SideEffect = SideEffect.DISK   # ★ 缺省保守
    #: 谁声明了这个契约（便于溯源与守卫比对）
    source: str = "builtin"
    description: str = ""
    #: P0 是否可执行
    executable: bool = True

    def required_params(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params if p.required)

    def param_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params)


# ---------------------------------------------------------------------------
# 从工具层派生 `tool.*`
# ---------------------------------------------------------------------------


def _side_effect_from_capability(name: str) -> SideEffect:
    """把工具层的三布尔注解**翻译**成编排层的副作用等级。

    ★ 缺省保守：能力注解取不到时按 `disk`（最危险）处理。
    """
    cap = tool_caps.CAPABILITIES.get(name)
    if cap is None:
        return SideEffect.DISK
    if cap.read_only and not cap.destructive:
        return SideEffect.NETWORK if cap.open_world else SideEffect.NONE
    return SideEffect.DISK


def _spec_from_tool_schema(tool_schema: dict) -> NodeSpec:
    """把 `TOOL_SCHEMAS` 里的一条 `{type, function{...}}` 转成 `NodeSpec`。"""
    fn = tool_schema.get("function") or {}
    name = str(fn.get("name") or "")
    raw_params = (fn.get("parameters") or {}).get("properties") or {}
    required = set((fn.get("parameters") or {}).get("required") or ())
    params: list[ParamSpec] = []
    for pname, pdef in raw_params.items():
        params.append(ParamSpec(
            name=pname,
            type=str((pdef or {}).get("type") or "string"),
            required=pname in required,
            description=str((pdef or {}).get("description") or ""),
        ))
    return NodeSpec(
        type=f"tool.{name}",
        params=tuple(params),
        side_effect=_side_effect_from_capability(name),
        source="core/tools/schemas.py",
        description=str(fn.get("description") or ""),
        executable=name in TOOL_HANDLERS,
    )


# ---------------------------------------------------------------------------
# 内置的非工具节点（这些是编排层自己的，不在 schemas.py 里）
# ---------------------------------------------------------------------------


def _builtin_specs() -> list[NodeSpec]:
    return [
        # ★ 感知节点（`docs/PERCEPTION.md` V1）——
        #   注意它**不是 `tool.*`**，所以**不会出现在 LLM 的工具清单里**
        #   （`TOOL_SCHEMAS` 未注册它）。这是刻意的：感知层未真正可用时，
        #   让模型能调一个必然失败的工具，只会制造无意义的失败轨迹。
        NodeSpec(
            type="perception.record",
            params=(
                ParamSpec("n_boxes", "integer", True,
                          description="已检测出的框数（V1 不含推理）"),
                ParamSpec("altitude_m", "number", True,
                          description="飞行高度（米）"),
                ParamSpec("calibration_path", "string", True,
                          description="校准模型 JSON 路径"),
                ParamSpec("camera", "string", False,
                          default="Mavic3Classic"),
                ParamSpec("tile_size_px", "integer", False, default=640),
                ParamSpec("tile_overlap", "number", False, default=0.15),
                ParamSpec("conf_threshold", "number", False, default=0.25),
                ParamSpec("nms_iou", "number", False, default=0.5),
                ParamSpec("weights_version", "string", False, default=""),
                ParamSpec("field_id", "string", False),
                ParamSpec("date", "string", False),
                ParamSpec("platform", "string", False),
                ParamSpec("detector_id", "string", False),
                ParamSpec("field_area_m2", "number", False),
                ParamSpec("invalid_tiles", "integer", False, default=0),
            ),
            side_effect=SideEffect.NONE,      # 纯计算，不写盘不联网
            source="core/orchestration/catalog.py",
            description="感知计数 → 契约记录（密度 + 校准区间）",
        ),
        NodeSpec(
            type="report.partial",
            params=(
                ParamSpec("question", "string", False,
                          description="原始问题（工作流输入透传）"),
                ParamSpec("done", "array", False,
                          description="已完成的调用列表"),
                ParamSpec("rounds_used", "integer", False, default=0),
                ParamSpec("tool_chain", "array", False),
                ParamSpec("out_dir", "string", False, default="reports"),
            ),
            side_effect=SideEffect.DISK,      # ★ 会写盘
            source="core/orchestration/catalog.py",
            description="把已完成的部分结果渲染成 Markdown 存档并落盘",
        ),
        NodeSpec(
            type="report.markdown",
            params=(
                ParamSpec("record", "string", True,
                          description="上游契约节点 id"),
                ParamSpec("decision", "string", False,
                          description="上游门控节点 id"),
                ParamSpec("out_dir", "string", False, default="reports"),
            ),
            side_effect=SideEffect.DISK,      # ★ 会写盘
            source="core/orchestration/catalog.py",
            description="把契约 + 门控结论渲染成 Markdown 报告并落盘",
        ),
        NodeSpec(
            type="data.kb_query",
            params=(
                ParamSpec("query", "string", True, description="检索语句（可含 {{输入}}）"),
                ParamSpec("top_k", "number", False, default=3),
            ),
            side_effect=SideEffect.NONE,          # 只读本地向量库
            source="core/orchestration/catalog.py",
            description="知识库检索（复用 core/knowledge_base.py）",
        ),
        NodeSpec(
            type="llm.decide",
            params=(
                ParamSpec("prompt_ref", "string", True, description="提示词文件路径"),
                ParamSpec("inputs", "array", False, description="上游节点 id 列表"),
                ParamSpec("output_schema", "object", True, description="输出契约（必须有）"),
            ),
            side_effect=SideEffect.NETWORK,       # 调模型接口
            source="core/orchestration/catalog.py",
            description="带输出契约的判定节点",
        ),
        NodeSpec(
            type="llm.summarize",
            params=(
                ParamSpec("prompt_ref", "string", True),
                ParamSpec("inputs", "array", False),
            ),
            side_effect=SideEffect.NETWORK,
            source="core/orchestration/catalog.py",
            description="带输出契约的摘要节点",
        ),
        NodeSpec(
            type="gate.numeric_consistency",
            params=(
                ParamSpec("sources", "array", True, description="上游节点 id"),
                ParamSpec("target", "string", True),
            ),
            side_effect=SideEffect.NONE,          # 纯校验，必须可重试
            source="core/orchestration/catalog.py",
            description="数值一致性门禁（Fail-Closed）",
        ),
        NodeSpec(
            type="gate.range",
            params=(
                ParamSpec("value", "string", True),
                ParamSpec("min", "number", False),
                ParamSpec("max", "number", False),
            ),
            side_effect=SideEffect.NONE,
            source="core/orchestration/catalog.py",
            description="范围门禁（越界即拒）",
        ),
        NodeSpec(
            type="gate.mutual_exclusion",
            params=(
                ParamSpec("a", "string", True),
                ParamSpec("b", "string", True),
            ),
            side_effect=SideEffect.NONE,
            source="core/orchestration/catalog.py",
            description="要素互斥门禁（两个标识互斥时拒绝）",
        ),
        NodeSpec(
            type="gate.agent_review",
            params=(
                # ★ `rubric` 与 `rubric_ref` 二选一（至少一个，由执行器校验）
                ParamSpec("rubric", "string", False,
                          description="审查要求（内联；与 rubric_ref 二选一）"),
                ParamSpec("rubric_ref", "string", False,
                          description="审查要求文件路径（与 rubric 二选一）"),
                ParamSpec("inputs", "array", False,
                          description="要审查的上游节点 id 列表"),
                ParamSpec("threshold", "number", False, default=0.7,
                          description="通过分数阈值（0-1）"),
            ),
            side_effect=SideEffect.NETWORK,   # 会调模型接口
            source="core/orchestration/catalog.py",
            description="AI 审查门禁：给上游产出打分，低于阈值即拒绝（第一道过滤，非人类终审）",
        ),
        NodeSpec(
            type="gate.uncertainty",
            params=(
                ParamSpec("lo", "number", True,
                          description="区间下界（可写 {{上游节点.字段}}）"),
                ParamSpec("hi", "number", True, description="区间上界"),
                ParamSpec("threshold", "number", True,
                          description="农艺阈值（高于=可采收）"),
                ParamSpec("value", "number", False,
                          description="点估计（仅用于算相对宽度；缺省取区间中点）"),
                ParamSpec("mode", "string", False, default="enforce",
                              description="enforce=硬拦 / annotate=只判不拦"),
                ParamSpec("max_rel_width", "number", False, default=0.30,
                          description="相对宽度上限，超过即视为「宽到没法用」"),
                ParamSpec("level", "string", False,
                          description="要求达到的最高档位（如 ADVISE）；未达到即拒绝"),
            ),
            side_effect=SideEffect.NONE,   # 纯计算，可重试
            source="core/orchestration/catalog.py",
            description="不确定性门控：按区间与阈值的相对位置判四档，不达标即拦",
        ),
        NodeSpec(
            type="human.confirm",
            params=(
                ParamSpec("prompt", "string", True),
                ParamSpec("max_reject", "number", False, default=3),
            ),
            side_effect=SideEffect.NONE,
            source="core/orchestration/catalog.py",
            description="人工确认（等待 + 落盘断点）",
        ),
    ]


# ---------------------------------------------------------------------------
# 目录（模块级，首次 import 时构建一次）
# ---------------------------------------------------------------------------


def _build() -> dict[str, NodeSpec]:
    specs: dict[str, NodeSpec] = {}
    for tool_schema in TOOL_SCHEMAS:
        spec = _spec_from_tool_schema(tool_schema)
        if spec.type != "tool.":
            specs[spec.type] = spec
    for spec in _builtin_specs():
        specs[spec.type] = spec
    return specs


#: `节点类型 -> NodeSpec`。**这是"哪些节点存在"的唯一真相。**
CATALOG: dict[str, NodeSpec] = _build()


def get(node_type: str) -> NodeSpec | None:
    return CATALOG.get(node_type)


def known_types() -> list[str]:
    return sorted(CATALOG)


def tool_node_types() -> list[str]:
    """从工具层派生的那些节点类型（供契约一致性守卫比对）。"""
    return sorted(t for t, s in CATALOG.items() if t.startswith("tool."))


def _designed_reason(node_type: str) -> str:
    """查"已设计但未实现"的理由。支持**通配符键**（`skill.*` / `notify.*` / ...）。"""
    if node_type in DESIGNED_NOT_IMPLEMENTED:
        return DESIGNED_NOT_IMPLEMENTED[node_type]
    fam = node_type.split(".", 1)[0]
    return DESIGNED_NOT_IMPLEMENTED.get(f"{fam}.*", "")


def describe(node_type: str) -> str:
    """给人看的说明（报错信息用）。"""
    spec = CATALOG.get(node_type)
    if spec is not None:
        return f"{node_type}（参数: {', '.join(spec.param_names()) or '无'}）"
    reason = _designed_reason(node_type)
    if reason:
        return f"{node_type} —— **已在文档中设计、但当前不可执行**：{reason}"
    fam = node_type.split(".", 1)[0]
    if fam in NODE_FAMILIES:
        near = [t for t in known_types() if t.startswith(fam + ".")][:4]
        return f"未知节点类型 {node_type!r}；`{fam}` 族可用的有: {near}"
    return (f"未知节点类型 {node_type!r}；已知族: {', '.join(NODE_FAMILIES)}。"
            f"完整清单: {known_types()}")


__all__ = [
    "CATALOG", "DESIGNED_NOT_IMPLEMENTED", "EXECUTABLE_TYPES", "NODE_FAMILIES",
    "NodeSpec", "describe", "get", "known_types", "tool_node_types",
]