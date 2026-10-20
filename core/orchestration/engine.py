# -*- coding: utf-8 -*-
"""工作流执行器 —— 把 `Workflow` 真正跑起来。

【P1 的可执行范围】**零 LLM**。当前真正能跑的节点：
        · `tool.*`   → `core.tools.execute_tool_result()`（**复用唯一分发入口**，
          于是能力闸门自动继承，不存在旁路）
        · `data.kb_query` → 知识库检索（只读）
        · `gate.*`   → `core.orchestration.gates`（Fail-Closed）

        `llm.*` 与 `human.confirm` **在 P1 明确抛 `NotSupportedError`** ——
        按决策 4(b)，`human.confirm` 延到 P2。**不谎报支持**比"能跑但语义不对"好。

【执行语义】（`docs/ORCHESTRATION.md` §5.3）
        · **调度**：拓扑排序 → 每轮取"入度已满足"的节点组成 ready batch → **批内并发**
        · **并发上限**：按节点类型切分，★ **matplotlib 渲染类必须串行**
        · **重试**：节点级 `retry` + 指数退避，★ **仅幂等节点可重试**
          （`model.Node.effective_retry` 已经把非幂等节点的重试强制取 0）
        · **超时**：节点级 `timeout_s`
        · **失败策略**：`fail_fast` / `degrade` / `skip_if_optional`
        · **熔断**：打回计数 ≥ `max_reject` → 终态 `blocked`

【为什么用线程而不是进程】节点是 I/O 密集（读 CSV、调工具、写盘），
        GIL 对它们不是瓶颈；而进程池会让"共享引擎实例/连接池/缓存"变得复杂。
        ★ 但 **matplotlib 必须串行** —— 这是 UI 层已经踩过的约束（STATUS_IN_PAGE_ERROR）。
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from core.orchestration import catalog, gates, store
from core.orchestration.model import (
    Node,
    Run,
    StepState,
    Workflow,
    Checkpoint,
)

#: ★ **必须串行**的节点类型。matplotlib 渲染在多线程下会崩
#: （`ui/main_window.py` 里有一段 `matplotlib 主线程预热` 的注释就是同一个原因）。
SERIAL_TYPES: frozenset[str] = frozenset({"tool.plot_trend"})

#: 串行锁（模块级 —— 同一进程内所有 engine 共用一把，因为底层库不是线程安全的）
_SERIAL_LOCK = threading.Lock()

#: 默认并发上限
DEFAULT_MAX_WORKERS = 4

#: 重试退避基数（秒）。第 n 次重试等 `BACKOFF_BASE * 2**(n-1)`。
BACKOFF_BASE = 0.2

#: 失败策略
ON_FAIL_POLICIES = frozenset({"fail_fast", "degrade", "skip_if_optional"})


class NotSupportedError(NotImplementedError):
    """该节点类型在当前阶段明确不支持（**不是 bug，是刻意的边界**）。"""


class EngineError(RuntimeError):
    """引擎级错误（工作流本身不合法、依赖缺失等）。"""


@dataclass
class NodeContext:
    """节点执行时可访问的上下文。

    `outputs` 是**已完成节点的产出**（含从断点恢复的），
    节点的 `params` 里可以用 `{{node_id}}` / `{{node_id.field}}` 引用它们。
    """

    inputs: dict[str, Any]
    outputs: dict[str, Any]
    run_id: str
    workflow_id: str

    def resolve(self, value: Any) -> Any:
        """把参数里的 `{{...}}` 占位符替换成实际值。

        支持 `{{region}}`（输入）、`{{node_a}}`（整份产出）、`{{node_a.field}}`（取字段）。
        ★ **解析不出来就原样保留** —— 让节点自己决定怎么处理，而不是静默变成空串
        （静默变空串是"写了但没生效"的经典来源）。
        """
        if isinstance(value, str):
            return self._resolve_str(value)
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        return value

    def _resolve_str(self, text: str) -> Any:
        import re
        whole = re.fullmatch(r"\{\{\s*([A-Za-z0-9_.:-]+)\s*\}\}", text)
        if whole:
            return self._lookup(whole.group(1), default=text)
        def repl(m: "re.Match[str]") -> str:
            got = self._lookup(m.group(1), default=None)
            return m.group(0) if got is None else str(got)
        return re.sub(r"\{\{\s*([A-Za-z0-9_.:-]+)\s*\}\}", repl, text)

    def _lookup(self, path: str, *, default: Any) -> Any:
        head, _, rest = path.partition(".")
        if head in self.outputs:
            cur = self.outputs[head]
        elif head in self.inputs:
            cur = self.inputs[head]
        else:
            return default
        for part in (rest.split(".") if rest else []):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur


@dataclass
class NodeOutcome:
    """一个节点的执行结果（交给引擎判定状态）。"""

    ok: bool
    output: Any = None
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: ★ 该节点需要**人工确认**：引擎应立即暂停、落盘断点、把控制权交回调用方。
    #: 这是 `human.confirm` 的语义 —— 不是失败，也不是完成，而是"等我签字"。
    waiting: bool = False


# ---------------------------------------------------------------------------
# 节点执行器注册表
# ---------------------------------------------------------------------------


def _exec_tool(node: Node, ctx: NodeContext) -> NodeOutcome:
    """执行 `tool.<name>` —— 走**唯一分发入口**，能力闸门自动生效。

    【产出结构】工具返回的是 **JSON 文本**。如果只把它塞进 `{"text": ...}`，
    下游就无法自然地引用字段（只能写 `{{node.text}}` 再去解析字符串）。
    所以这里**把 JSON 字段展开到产出顶层** → 下游可以写 `{{chart.chart_path}}`。

    【图片】`ToolResult.images` 是 **`list[(png_bytes, mime)]`**（不是裸 bytes）。
    ★ 而且 `plot_trend` 默认 `_materialize=True`，**它自己已经把图写到磁盘**并把
    `chart_path` 放进了 JSON —— 引擎**不要再写一份**，否则同一张图会出现两个副本。
    只有当工具**没**物化（拿不到 `chart_path`）时，引擎才兜底把字节落盘。
    """
    from core.tools import execute_tool_result
    import json as _json

    name = node.type.split(".", 1)[1]
    args = {k: ctx.resolve(v) for k, v in node.params.items()}
    result = execute_tool_result(name, args)
    text = getattr(result, "text", "") or str(result)

    output: dict[str, Any] = {"text": text, "image_path": None, "image_paths": []}

    # ① 先把 JSON 字段展开到顶层（★ 不覆盖 text / image_path / image_paths）
    try:
        payload = _json.loads(text)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in ("text", "image_path", "image_paths"):
                continue
            output.setdefault(k, v)
    elif isinstance(payload, list):
        output.setdefault("items", payload)

    # ② 图片：优先用工具**已经物化**的路径（`chart_path`），不要重复写
    if output.get("chart_path"):
        output["image_path"] = output["chart_path"]
        output["image_paths"] = [output["chart_path"]]
    else:
        images = list(getattr(result, "images", None) or [])
        if images:
            saved = _save_images(images, run_id=ctx.run_id, node_id=node.id)
            output["image_paths"] = saved
            output["image_path"] = saved[0] if saved else None

    return NodeOutcome(ok=True, output=output)


def _save_images(images: list[Any], *, run_id: str, node_id: str) -> list[str]:
    """把图片字节写到图表目录（**兜底路径**，仅在工具未物化时使用）。

    ★ `images` 的元素是 `(bytes, mime)` 元组 —— 与 `ToolResult` 的约定一致。
    ★ 用 `core.trend_plot.next_chart_path()` 取路径，**与工具层共用同一套命名与目录**，
    不自己拼一个 `charts/` 硬路径（第一版这么干，结果 `config.CHART_DIR` 压根不存在）。
    """
    from core.trend_plot import next_chart_path

    paths: list[str] = []
    for item in images:
        blob = item[0] if isinstance(item, (tuple, list)) and item else item
        if not isinstance(blob, (bytes, bytearray)):
            continue
        p = next_chart_path(f"orch_{node_id}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes(blob))
        paths.append(p.as_posix())
    return paths


def _exec_kb_query(node: Node, ctx: NodeContext) -> NodeOutcome:
    """`data.kb_query` —— 只读检索。**引擎持有一个知识库实例以避免重复加载**。"""
    kb = ctx.inputs.get("__kb__")
    if kb is None:
        raise EngineError("data.kb_query 需要注入知识库实例（inputs['__kb__']）")
    q = ctx.resolve(node.params.get("query", ""))
    top_k = int(ctx.resolve(node.params.get("top_k", 3)) or 3)
    docs = kb.search(q, top_k=top_k)
    return NodeOutcome(ok=True, output={"docs": docs, "query": q})


#: 档位从"最保守"到"最放行"的顺序，用于判断"是否达到要求档位"。
_LEVEL_ORDER = ("ESCALATE", "QUALITATIVE", "ADVISE_WITH_INTERVAL", "ADVISE")


def _exec_gate_uncertainty(params: dict) -> NodeOutcome:
    """gate.uncertainty —— 按区间判四档，并用 level 参数决定拦不拦。

    语义：level 写的是"**最低可接受档位**"。例如写 level: ADVISE
    表示"只有能正常建议时才放行" —— 低于它（降级/弃答）就**拒绝**，让工作流停在这里。

    ★ **两种用法，由 `mode` 显式选择**：

      · `mode: enforce`（默认）—— **硬拦**。用在"要拿这个数去做不可逆操作"之前：
        `must_abstain` 一律拒绝；给了 `level` 则未达最低档位也拒绝。
      · `mode: annotate` —— **只判不拦**。用在"要出报告说明信息不足"时：
        弃答也要让下游把"信息不足"这个**结论本身**写出来，
        拦住反而会连报告都没有。

    【为什么必须显式给 mode 而不是靠"写不写 level"推断】
      原实现写的是"不写 level 时只判不拦"，但代码里 `must_abstain`
      会**无条件拒绝** —— 文档与实现矛盾，且无法表达"弃答但仍要出报告"这一需求。
      改成显式参数后，两种用法都说得清，也不会因省略参数而静默改变行为。
    """
    from core import uncertainty as U

    try:
        lo = params.get("lo")
        hi = params.get("hi")
        threshold = float(params.get("threshold"))
    except (TypeError, ValueError) as e:
        return NodeOutcome(ok=False, error=f"gate.uncertainty 参数非法：{e}")

    value = params.get("value")
    try:
        value_f = float(value) if value is not None else None
    except (TypeError, ValueError):
        value_f = None
    try:
        max_rel = float(params.get("max_rel_width", U.DEFAULT_MAX_REL_WIDTH))
    except (TypeError, ValueError):
        max_rel = U.DEFAULT_MAX_REL_WIDTH

    decision = U.evaluate(lo=lo, hi=hi, threshold=threshold,
                          value=value_f, max_rel_width=max_rel)
    out = {
        "level": decision.level.value,
        "side": decision.side.value,
        "reason": decision.reason,
        "interval": [decision.lo, decision.hi],
        "threshold": decision.threshold,
        "rel_width": decision.rel_width,
        # 供下游组装提示词用（"LLM 不可越权硬门"的前置半边）
        "constraint": U.constraint_text(decision),
    }

    # ① `mode=annotate`：只判不拦（弃答也要让下游写出"信息不足"这个结论）
    mode = str(params.get("mode") or "enforce").strip().lower()
    if mode not in ("enforce", "annotate"):
        return NodeOutcome(
            ok=False,
            error=f"未知 mode {mode!r}；可选：['enforce', 'annotate']")
    out["mode"] = mode
    if mode == "annotate":
        return NodeOutcome(ok=True, output=out)

    # ② 弃答是硬要求：不可被 level 参数绕过
    if decision.must_abstain:
        return NodeOutcome(ok=False, error=f"不确定性门控：{decision.reason}",
                           detail=out, output=out)

    # ③ 指定了最低档位 → 未达到就拒绝
    want = str(params.get("level") or "").strip().upper()
    if want:
        if want not in _LEVEL_ORDER:
            return NodeOutcome(ok=False,
                               error=f"未知档位要求 {want!r}；可选：{list(_LEVEL_ORDER)}")
        if _LEVEL_ORDER.index(decision.level.value) < _LEVEL_ORDER.index(want):
            return NodeOutcome(
                ok=False,
                error=(f"档位不足：实际 {decision.level.value}，要求 ≥ {want}。"
                       f"{decision.reason}"),
                detail=out, output=out)

    return NodeOutcome(ok=True, output=out)


def _exec_gate(node: Node, ctx: NodeContext) -> NodeOutcome:
    """执行 `gate.*` —— **Fail-Closed**。

    ★ 门禁"不通过"对引擎来说是**节点失败**，不是"跳过"。
    因为门禁的语义就是"这里不对就别往下走"。
    """
    kind = node.type
    params = {k: ctx.resolve(v) for k, v in node.params.items()}

    # ★ gate.uncertainty 不走确定性门禁表（gates.py），单独处理：
    # 它判的是"区间与阈值的关系"，输出是**四档结论**而不只是通过/拒绝。
    if kind == "gate.uncertainty":
        return _exec_gate_uncertainty(params)
    # YAML 里的键名映射到 gates 的函数签名
    if kind == "gate.numeric_consistency":
        sources = params.pop("sources", [])
        if isinstance(sources, str):
            sources = [sources]
        # sources 里可能写的是节点 id → 取出它们的产出
        resolved = [ctx.outputs.get(s, s) if isinstance(s, str) else s for s in sources]
        res = gates.numeric_consistency(
            resolved, params.pop("target", None),
            tolerance=float(params.pop("tolerance", 0.01) or 0.01))
    elif kind == "gate.range":
        res = gates.range_check(
            params.pop("value", None),
            minimum=_maybe_float(params.pop("min", None), params.pop("minimum", None)),
            maximum=_maybe_float(params.pop("max", None), params.pop("maximum", None)))
    elif kind == "gate.mutual_exclusion":
        res = gates.mutual_exclusion(params.pop("a", None), params.pop("b", None))
    else:
        return NodeOutcome(ok=False, error=f"未实现的门禁类型 {kind}")

    if not res.passed:
        return NodeOutcome(ok=False, error=str(res.reason), detail=res.details)
    return NodeOutcome(ok=True, output={"passed": True, "reason": res.reason})


def _maybe_float(*candidates: Any) -> float | None:
    for c in candidates:
        if c is None or c == "":
            continue
        try:
            return float(c)
        except (TypeError, ValueError):
            continue
    return None


def _exec_llm(node: Node, ctx: NodeContext) -> NodeOutcome:
    """`llm.decide` / `llm.summarize` —— **带输出契约的模型节点**。

    【为什么必须有输出契约】`llm.*` 的产出要给下游节点用，所以必须是**结构化**的。
    没有契约 = 下游拿到一段自由文本 = 只能靠字符串猜 —— 那正是本项目一直在消除的
    "两份地图"问题。所以 `output_schema` 是**必填参数**（`catalog.py` 已声明）。

    【Fail-Closed】模型输出必须能解析成 JSON **且包含 schema 声明的全部字段**。
    缺字段就判本节点失败，而不是给下游一个 `None` 让它自己想办法。
    """
    import json as _json
    from pathlib import Path as _Path

    params = {k: ctx.resolve(v) for k, v in node.params.items()}
    prompt_ref = str(params.get("prompt_ref") or "").strip()
    if not prompt_ref:
        return NodeOutcome(ok=False, error="llm 节点缺少 prompt_ref")
    schema = params.get("output_schema") or {}
    if not isinstance(schema, dict) or not schema:
        return NodeOutcome(ok=False, error="llm 节点缺少 output_schema（无契约不接受）")

    # ★ 2026-10-20 修：绝对路径照用；相对路径**必须**走 `resource_path`。
    #   原来写的是 `Path(__file__).parent.parent.parent / prompt_ref` ——
    #   打包后 `__file__` 在 `_MEIPASS` 内，这个推导必然指错，
    #   于是**任何 `llm.decide` / `llm.summarize` 节点在打包版都会失败**
    #   （`prompt_ref` 是 required 参数，绕不过去）。
    if _Path(prompt_ref).is_absolute():
        p = _Path(prompt_ref)
    else:
        from config import resource_path
        p = resource_path(prompt_ref)
    if not p.is_file():
        return NodeOutcome(ok=False, error=f"提示词文件不存在: {p}")
    prompt_text = p.read_text(encoding="utf-8")

    upstream_ids = params.get("inputs") or []
    if isinstance(upstream_ids, str):
        upstream_ids = [upstream_ids]
    blocks: list[str] = []
    for uid in upstream_ids:
        val = ctx.outputs.get(str(uid))
        blocks.append(f"### 上游节点 {uid}\n"
                      f"{_json.dumps(val, ensure_ascii=False, default=str)[:4000]}")
    for k, v in ctx.inputs.items():
        prompt_text = prompt_text.replace("{{" + k + "}}", str(v))

    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content":
            (("以下是上游节点的产出：\n\n" + "\n\n".join(blocks)) if blocks else "请开始。")
            + f"\n\n【输出要求】只输出一个 JSON 对象，必须且只包含这些字段："
              f"{_json.dumps(schema, ensure_ascii=False)}。"
              + "\n不要输出 Markdown 代码块，不要解释，不要多余字段。"},
    ]

    from core import providers
    client = providers.default_client()
    raw = client.generate_response(messages) or ""

    text = raw.strip()
    if text.startswith("```"):                      # 容忍 ```json 围栏
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        data = _json.loads(text)
    except (ValueError, TypeError) as e:
        return NodeOutcome(ok=False,
                           error=f"模型输出不是合法 JSON：{e}；原文前 200 字：{raw[:200]}")
    if not isinstance(data, dict):
        return NodeOutcome(ok=False,
                           error=f"模型输出不是 JSON 对象，而是 {type(data).__name__}")

    missing = [k for k in schema if k not in data]
    if missing:
        return NodeOutcome(ok=False,
                           error=f"模型输出缺少契约字段 {missing}；实际字段 {sorted(data)}")

    return NodeOutcome(ok=True, output=data)


def _exec_human_confirm(node: Node, ctx: NodeContext) -> NodeOutcome:
    """`human.confirm` —— **暂停并等待人工签字**。

    ★ 它不是"跑完"也不是"失败"：返回 `waiting=True`，
    引擎据此**立刻停止调度、落盘断点、把控制权交回调用方**。
    之后由 `Engine.decide(run_id, approved=...)` 续跑。

    `max_reject` 是熔断阈值：连续打回这么多次 → 终态 `blocked`，**不回退到别的分支**
    （`docs/ORCHESTRATION.md` §5.3）。
    """
    params = {k: ctx.resolve(v) for k, v in node.params.items()}
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return NodeOutcome(ok=False, error="human.confirm 缺少 prompt 文本")
    try:
        max_reject = int(params.get("max_reject", 3) or 3)
    except (TypeError, ValueError):
        max_reject = 3
    return NodeOutcome(
        ok=False,           # 不是完成 —— 引擎按 `waiting` 单独处理
        waiting=True,
        output={"prompt": prompt, "max_reject": max_reject},
        error="",
    )


def _exec_gate_review(node: Node, ctx: NodeContext) -> NodeOutcome:
    """`gate.agent_review` —— **让模型当审查员**，给上游产出打分并决定拦不拦。

    ★★ **它不是人类终审。** `docs/ABSORPTION.md` §5.3 记过一条机制事实：
    YY-Flow 的「已完成→已验收」**仅人类专属，严禁 Agent 代签**。
    所以本节点是**第一道过滤**：早点把明显不合格的产出拦下来，
    但**通过它不等于被批准** —— 终审仍然只能由 `human.confirm` 表达。

    【Fail-Closed】以下情形**全部拒绝**（与确定性门禁同一条纪律）：
      · 既没 `rubric` 也没 `rubric_ref`；`rubric_ref` 指向的文件不存在
      · 模型输出解析不成 JSON / 缺字段 / score 不是数字
      · 分数低于阈值；或模型说「通过」但分数不够（**不采信自相矛盾的结论**）
      · 执行器自身异常

    ★ **它是「会花钱的门禁」**：`side_effect: network`，每跑一次调一次模型。
    所以文档把它标成「中量 LLM」，而不是零成本。
    """
    import json as _json
    from pathlib import Path as _Path

    params = {k: ctx.resolve(v) for k, v in node.params.items()}

    # ① 审查要求：内联 or 文件
    rubric = str(params.get("rubric") or "").strip()
    rubric_ref = str(params.get("rubric_ref") or "").strip()
    if not rubric and not rubric_ref:
        return NodeOutcome(ok=False, error="agent_review 需要 rubric 或 rubric_ref（无标准不审查）")
    if not rubric and rubric_ref:
        # ★ 2026-10-20 修：与 `prompt_ref` 同一个错误模式（`__file__` 推导），
        #   打包后 `gate.agent_review` 用文件 rubric 时必然失败。
        if _Path(rubric_ref).is_absolute():
            rp = _Path(rubric_ref)
        else:
            from config import resource_path
            rp = resource_path(rubric_ref)
        if not rp.is_file():
            return NodeOutcome(ok=False, error=f"审查要求文件不存在: {rp}")
        rubric = rp.read_text(encoding="utf-8")

    # ② 阈值
    try:
        threshold = float(params.get("threshold", 0.7))
    except (TypeError, ValueError):
        return NodeOutcome(ok=False, error=f"threshold 不是数字: {params.get('threshold')!r}")
    if not (0.0 <= threshold <= 1.0):
        return NodeOutcome(ok=False, error=f"threshold 应在 0-1 之间，收到 {threshold}")

    # ③ 被审查的内容
    targets = params.get("inputs") or []
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        return NodeOutcome(ok=False, error="agent_review 需要 inputs（要审查哪些上游节点）")
    blocks: list[str] = []
    for tb in targets:
        val = ctx.outputs.get(str(tb))
        if val is None:
            return NodeOutcome(ok=False,
                               error=f"被审查的节点 {tb} 还没有产出（或名字拼错）")
        blocks.append(f"### 待审内容（来自节点 {tb}）\n"
                      f"{_json.dumps(val, ensure_ascii=False, default=str)[:6000]}")

    messages = [
        {"role": "system", "content":
            "你是严格的产出审查员。请依据【审查标准】判断【待审内容】是否合格。\n"
            "只输出一个 JSON 对象，字段：\n"
            '  "passed": true 或 false（是否合格）\n'
            '  "score": 0 到 1 之间的小数（合格程度）\n'
            '  "reason": 一句话说明理由（不合格时必须指出具体问题）\n'
            "不要输出 Markdown 代码块，不要解释，不要多余字段。\n\n"
            f"【审查标准】\n{rubric}"},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]

    from core import providers
    client = providers.default_client()
    raw = client.generate_response(messages) or ""

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        verdict = _json.loads(text)
    except (ValueError, TypeError) as e:
        return NodeOutcome(ok=False,
                           error=f"审查员输出不是合法 JSON：{e}；原文前 200 字：{raw[:200]}")
    if not isinstance(verdict, dict):
        return NodeOutcome(ok=False, error="审查员输出不是 JSON 对象")

    missing = [k for k in ("passed", "score", "reason") if k not in verdict]
    if missing:
        return NodeOutcome(ok=False,
                           error=f"审查员输出缺少字段 {missing}；实际 {sorted(verdict)}")
    try:
        score = float(verdict["score"])
    except (TypeError, ValueError):
        return NodeOutcome(ok=False, error=f"审查员给的 score 不是数字: {verdict['score']!r}")

    reason = str(verdict.get("reason") or "")
    passed_flag = bool(verdict["passed"])

    # ★ 双重判据：模型说「通过」但分数不够 → 仍然拒绝（不采信自相矛盾的结论）
    if not passed_flag or score < threshold:
        return NodeOutcome(
            ok=False,
            error=f"审查未通过（score={score:.2f} < 阈值 {threshold:.2f}）："
                  f"{reason or '（审查员未给理由）'}",
            detail={"score": score, "threshold": threshold,
                    "reviewer_passed": passed_flag, "reason": reason})

    return NodeOutcome(ok=True, output={
        "passed": True, "score": score, "reason": reason, "threshold": threshold,
        "note": "AI 审查通过 —— **不构成人类终审**；如需签字请用 human.confirm",
    })


_UNRESOLVED_RE = re.compile(r"^\{\{\s*[A-Za-z0-9_.:-]+\s*\}\}$")


def _unresolved(v: Any) -> bool:
    """这个值是不是**没解析成功的占位符**（如 `{{field_area_m2}}`）。

    ★ 为什么需要这个判断：`NodeContext.resolve()` 的既定语义是
      **"解析不出来就原样保留"**（刻意如此 —— 静默变空串会让"写了但没生效"
      的 bug 无声无息）。所以**可选输入没提供时**，参数拿到的是字面量
      `"{{field_area_m2}}"` 而不是 `None`。

      任何把参数当数字/当路径用的节点**都必须先过这一关**，否则会得到
      `could not convert string to float: '{{field_area_m2}}'` ——
      一个看不出因果的错误。
    """
    return isinstance(v, str) and bool(_UNRESOLVED_RE.match(v.strip()))


def _opt(params: dict, name: str) -> Any:
    """取可选参数：缺失或未解析 → `None`。"""
    v = params.get(name)
    return None if v is None or _unresolved(v) else v


def _opt_num(params: dict, name: str) -> float | None:
    v = _opt(params, name)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise EngineError(f"参数 {name} 不是有效数值：{v!r}") from None


def _opt_int(params: dict, name: str, default: int = 0) -> int:
    v = _opt_num(params, name)
    return default if v is None else int(v)


def _opt_str(params: dict, name: str, default: str = "") -> str:
    v = _opt(params, name)
    return default if v is None else str(v)


def _exec_perception_record(node: Node, ctx: NodeContext) -> NodeOutcome:
    """`perception.record` —— 框数 + 高度 + 校准模型 → 契约记录。

    ★ V1 **不含 YOLO 推理**（见 `docs/PERCEPTION.md`）。输入是已检测的框数。
    ★ 拿不到校准模型就**失败**，不内置"演示用"假校准 ——
      假区间会一路流进门控，让系统做出看起来有依据、实际无依据的决策。
    """
    from core.perception.pipeline import PipelineError, build_record, load_calibration

    params = {k: ctx.resolve(v) for k, v in node.params.items()}

    def _req(name: str):
        v = params.get(name)
        if v is None or v == "":
            raise EngineError(f"perception.record 缺少必需参数 {name}")
        return v

    try:
        cal = load_calibration(str(_req("calibration_path")))
        rec = build_record(
            n_boxes=int(_req("n_boxes")),
            altitude_m=float(_req("altitude_m")),
            calibration=cal,
            camera=_opt_str(params, "camera", "Mavic3Classic"),
            tile_size_px=_opt_int(params, "tile_size_px", 640),
            tile_overlap=(_opt_num(params, "tile_overlap") or 0.15),
            conf_threshold=(_opt_num(params, "conf_threshold") or 0.25),
            nms_iou=(_opt_num(params, "nms_iou") or 0.5),
            weights_version=_opt_str(params, "weights_version"),
            field_id=_opt_str(params, "field_id") or None,
            date=_opt_str(params, "date") or None,
            platform=_opt_str(params, "platform") or None,
            detector_id=_opt_str(params, "detector_id") or None,
            field_area_m2=_opt_num(params, "field_area_m2"),
            invalid_tiles=_opt_int(params, "invalid_tiles", 0),
        )
    except PipelineError as e:
        return NodeOutcome(ok=False, error=f"感知管线失败：{e}")

    errors, warnings = rec.validate()
    if errors:
        # ★ Fail-Closed：没有可决策的内容就**不往下走**（否则门控拿到 None 区间）
        return NodeOutcome(ok=False, error="契约不可决策：" + "；".join(errors))

    lo, hi = rec.density_interval_per_m2
    out = {
        "record": rec.to_dict(),
        "lo": lo, "hi": hi,
        "density": rec.density_hat_per_m2,
        "warnings": warnings,
    }
    if warnings:
        # 警告不阻塞，但必须让下游/日志看得见
        print(f"[perception.record] ★ {len(warnings)} 条警告：{warnings[:2]}")
    return NodeOutcome(ok=True, output=out)


def _exec_report_markdown(node: Node, ctx: NodeContext) -> NodeOutcome:
    """`report.markdown` —— 契约 + 门控结论 → Markdown 报告落盘。"""
    import json as _json

    from core.perception import report as _report
    from core.perception.contract import PerceptionRecord

    params = {k: ctx.resolve(v) for k, v in node.params.items()}
    rid = str(params.get("record") or "")
    if not rid:
        raise EngineError("report.markdown 缺少参数 record（上游节点 id）")
    up = ctx.outputs.get(rid)
    if not isinstance(up, dict) or not isinstance(up.get("record"), dict):
        return NodeOutcome(ok=False,
                           error=f"上游 {rid} 没有契约记录（record 字段）")

    # 从 dict 还原契约对象（不用 __init__，避免字段增删导致崩溃）
    raw = dict(up["record"])
    rec = PerceptionRecord()
    for k, v in raw.items():
        if k == "inference_spec" and isinstance(v, dict):
            from core.perception.contract import InferenceSpec
            v = InferenceSpec(**{kk: v[kk] for kk in v
                                 if kk in InferenceSpec.__dataclass_fields__})
        elif k == "density_interval_per_m2" and isinstance(v, list) and len(v) == 2:
            v = (v[0], v[1])
        if k in PerceptionRecord.__dataclass_fields__:
            setattr(rec, k, v)

    # 门控结论（可选）
    #
    # ★ 这里**不能静默吞异常**：曾经写成 `except: decision = None`，
    #   结果因为漏传 `threshold`（GateDecision 的必填字段），决策段
    #   被悄悄丢掉 —— 报告看起来正常，但**最关键的结论没了**。
    #   所以构造失败必须返回错误，让工作流停在这里。
    decision = None
    did = str(params.get("decision") or "")
    if did:
        dout = ctx.outputs.get(did)
        if not isinstance(dout, dict):
            return NodeOutcome(
                ok=False, error=f"上游门控 {did} 没有产出（缺少 level/threshold）")
        from core.uncertainty import GateDecision, Level, Side
        try:
            decision = GateDecision(
                level=Level(str(dout.get("level") or "")),
                side=Side(str(dout.get("side") or "")),
                reason=str(dout.get("reason") or ""),
                threshold=float(dout["threshold"]),
                lo=dout.get("interval", [None, None])[0]
                if isinstance(dout.get("interval"), list) else None,
                hi=dout.get("interval", [None, None])[1]
                if isinstance(dout.get("interval"), list)
                and len(dout["interval"]) > 1 else None,
                rel_width=(float(dout["rel_width"])
                           if dout.get("rel_width") is not None else None),
            )
        except (KeyError, TypeError, ValueError) as e:
            return NodeOutcome(
                ok=False,
                error=("门控结论无法还原为决策对象："
                       f"{type(e).__name__}: {e}（上游产出："
                       f"{sorted(dout)}）"))

    out_dir = _opt_str(params, "out_dir", "reports") or "reports"
    try:
        path = _report.write_report(rec, decision, out_dir=out_dir,
                                    extra_notes=list(up.get("warnings") or []))
    except Exception as e:                                   # noqa: BLE001
        return NodeOutcome(ok=False, error=f"报告生成失败：{type(e).__name__}: {e}")

    md = path.read_text(encoding="utf-8")
    return NodeOutcome(ok=True, output={
        "path": str(path), "markdown": md, "bytes": len(md.encode("utf-8"))})


def _exec_report_partial(node: Node, ctx: NodeContext) -> NodeOutcome:
    """`report.partial` —— 把已完成的部分结果渲染成 Markdown 存档并落盘。

    ★ 它是移交工作流的**第二步**（第一步是 `human.confirm`）：
      人确认要继续之后，把手上已有的中间结果固化成一份可带走的文件。
      ★ `side_effect: disk` —— 会写盘，所以它走的是"任务层"的审批路径。
    """
    from core import handoff as _handoff

    params = {k: ctx.resolve(v) for k, v in node.params.items()}

    def _as_list(v):
        """参数可能是真 list（模板解析成功），也可能是字符串（未解析或 JSON 文本）。"""
        if isinstance(v, list):
            return v
        if isinstance(v, str) and v.strip():
            import json as _json
            try:
                got = _json.loads(v)
                return got if isinstance(got, list) else [v]
            except (ValueError, TypeError):
                return [v]
        return []

    question = _opt_str(params, "question")
    done = _as_list(_opt(params, "done"))
    chain = _as_list(_opt(params, "tool_chain"))
    rounds = _opt_int(params, "rounds_used", 0)
    out_dir = _opt_str(params, "out_dir", "reports") or "reports"

    md = _handoff.render_partial_archive({
        "question": question, "done": done,
        "rounds_used": rounds, "tool_chain": chain,
    })

    from pathlib import Path as _P
    p = _P(out_dir) / f"partial-{ctx.run_id}.md"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
    except OSError as e:
        return NodeOutcome(ok=False, error=f"存档写入失败：{e}")

    return NodeOutcome(ok=True, output={
        "path": str(p), "markdown": md,
        "bytes": len(md.encode("utf-8")), "steps": len(done)})


def _exec_not_supported(node: Node, ctx: NodeContext) -> NodeOutcome:
    raise NotSupportedError(f"节点类型 {node.type} 在当前实现中不可执行")


#: `节点类型 → 执行函数`。按前缀匹配（`tool.` / `gate.` 是族）。
_EXECUTORS: dict[str, Callable[[Node, NodeContext], NodeOutcome]] = {
    "tool": _exec_tool,
    "data.kb_query": _exec_kb_query,
    "gate": _exec_gate,
    # ★ 精确匹配优先于族前缀 —— `gate.agent_review` 要调模型，不能走确定性门禁
    "gate.agent_review": _exec_gate_review,
    "llm": _exec_llm,
    "human.confirm": _exec_human_confirm,
    # ★ 感知与报告（`docs/PERCEPTION.md`）—— 精确匹配，
    #   不走 `tool.` 族前缀，因此**不会**被 LLM 当成工具调用
    "perception.record": _exec_perception_record,
    "report.markdown": _exec_report_markdown,
    "report.partial": _exec_report_partial,
}


def executor_for(node_type: str) -> Callable[[Node, NodeContext], NodeOutcome] | None:
    """按节点类型找执行函数（先精确、再族前缀）。"""
    if node_type in _EXECUTORS:
        return _EXECUTORS[node_type]
    fam = node_type.split(".", 1)[0]
    if fam in _EXECUTORS:
        return _EXECUTORS[fam]
    if node_type in catalog.DESIGNED_NOT_IMPLEMENTED:
        return _exec_not_supported
    return None


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class Engine:
    """工作流执行器。

    `run()` 返回填好的 `Run`；若给出 `store`，每完成一个节点就落盘
    （这样进程被杀也只会丢最后一步，而不是全丢）。
    """

    def __init__(
        self,
        workflow: Workflow,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        persist: bool = True,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.wf = workflow
        self.max_workers = max(1, int(max_workers))
        self.persist = persist
        self.on_event = on_event
        self._nodes = workflow.node_map()

    # ---- 对外主入口 ----
    def run(
        self,
        inputs: dict[str, Any] | None = None,
        *,
        run_id: str = "",
        resume: bool = False,
    ) -> Run:
        """执行工作流。

        `resume=True` 时从断点恢复：**已完成的节点直接取断点里的产出，不重新执行**。
        """
        run = Run(
            run_id=run_id or store.new_run_id(self.wf.id),
            workflow_id=self.wf.id,
            state=StepState.RUNNING,
            inputs=dict(inputs or {}),
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        ctx = NodeContext(inputs=run.inputs, outputs={}, run_id=run.run_id,
                          workflow_id=self.wf.id)

        if resume:
            cp = store.load_checkpoint(run.run_id)
            if cp is not None:
                ctx.outputs.update(cp.node_outputs or {})
                for nid, st in (cp.node_states or {}).items():
                    s = run.step(nid)
                    try:
                        s.state = StepState(st)
                    except ValueError:
                        s.state = StepState.PENDING
                    s.output = ctx.outputs.get(nid)
                self._emit({"event": "resumed", "run_id": run.run_id,
                            "restored": len(ctx.outputs)})

        pending = [n for n in self.wf.nodes
                   if run.step(n.id).state == StepState.PENDING]
        # 完成/失败/熔断的节点不再调度（终态）
        while pending:
            batch = self._ready_batch(pending, run)
            if not batch:
                stuck = [n.id for n in pending]
                raise EngineError(
                    f"无法继续调度：剩余节点 {stuck} 的依赖永远无法满足"
                    f"（可能是环或上游已失败）")
            self._run_batch(batch, run, ctx)
            self._persist(run, ctx)
            # ★ 有人工确认节点在等 → 立即暂停，把控制权交回调用方
            if self._waiting_node(run):
                run.state = StepState.WAITING
                self._persist(run, ctx, waiting_node=self._waiting_node(run) or "")
                self._emit({"event": "waiting", "run_id": run.run_id,
                            "node": self._waiting_node(run)})
                return run
            # 失败策略可能让整轮提前终止
            if self._should_abort(run):
                break
            pending = [n for n in self.wf.nodes
                       if run.step(n.id).state == StepState.PENDING]

        run.state = self._final_state(run)
        self._persist(run, ctx)
        self._emit({"event": "finished", "run_id": run.run_id,
                    "state": run.state.value})
        return run

    # ---- 人工确认的续跑 ----
    def _waiting_node(self, run: Run) -> str:
        for nid, s in run.steps.items():
            if s.state == StepState.WAITING:
                return nid
        return ""

    @staticmethod
    def _waiting_node_ckpt(cp: Checkpoint) -> str:
        """从断点的 `node_states` 里找 WAITING 节点（兼容 `waiting_node` 为空的老断点）。"""
        for nid, st in (cp.node_states or {}).items():
            if st == StepState.WAITING.value:
                return nid
        return ""

    def decide(self, run_id: str, *, approved: bool,
               inputs: dict[str, Any] | None = None) -> Run:
        """给 `human.confirm` 一个答复，然后**从断点续跑**。

        · `approved=True`  → 该节点 DONE，继续往后跑；
        · `approved=False` → 打回计数 +1；未达 `max_reject` 则**继续等**；
          达到 `max_reject` → 终态 `blocked`（熔断），**不换分支、不重试**。

        ★ 打回计数存在 `Step.rejects` 里并随断点落盘 —— 否则
        "连续打回 3 次"这个约束在重启后就失效了。

        ★★ `inputs` 用于**重新注入运行时对象**（如知识库实例 `__kb__`）。
        这类对象**天生无法持久化** —— 断点里只能存它们的字符串形式。
        续跑时调用方必须把它们再传一次，否则下游节点会炸（守卫实测抓到的真问题）。
        """
        cp = store.load_checkpoint(run_id)
        if cp is None:
            raise EngineError(f"找不到运行 {run_id} 的断点，无法续跑")
        waiting = cp.waiting_node or self._waiting_node_ckpt(cp)
        if not waiting:
            raise EngineError(f"运行 {run_id} 没有处于等待的节点")

        node = self._nodes.get(waiting)
        if node is None:
            raise EngineError(f"断点里的节点 {waiting} 不在本工作流中")

        run = store.load_run(run_id) or Run(
            run_id=run_id, workflow_id=self.wf.id, inputs=dict(cp.node_outputs))
        step = run.step(waiting)

        max_reject = 3
        if isinstance(step.output, dict):
            max_reject = int(step.output.get("max_reject", 3) or 3)

        if not approved:
            step.rejects += 1
            if step.rejects >= max_reject:
                # ★ 熔断：终态 blocked，不回退
                step.state = StepState.BLOCKED
                step.error = f"人工连续打回 {step.rejects} 次（上限 {max_reject}），已熔断"
                run.state = StepState.BLOCKED
                self._persist(run, NodeContext(
                    inputs=cp.node_outputs, outputs=cp.node_outputs,
                    run_id=run_id, workflow_id=self.wf.id))
                self._emit({"event": "blocked", "run_id": run_id, "node": waiting,
                            "rejects": step.rejects})
                return run
            # 未到上限 → 继续等，让调用方再决定一次
            run.state = StepState.WAITING
            ctx = NodeContext(inputs=run.inputs, outputs=dict(cp.node_outputs),
                              run_id=run_id, workflow_id=self.wf.id)
            self._persist(run, ctx, waiting_node=waiting)
            self._emit({"event": "rejected", "run_id": run_id, "node": waiting,
                        "rejects": step.rejects, "max_reject": max_reject})
            return run

        # 通过 → 该节点完成，继续
        # ★ 顺序很重要：必须**先把「已完成」写进断点**，再 resume。
        # 否则 `run(resume=True)` 会从**旧断点**恢复出 WAITING，导致原地打转。
        step.rejects = step.rejects
        step.state = StepState.DONE
        step.output = {"approved": True, **(
            step.output if isinstance(step.output, dict) else {})}
        run.state = StepState.RUNNING
        outputs = dict(cp.node_outputs)
        outputs[waiting] = step.output          # 下游要能拿到它的产出
        ctx = NodeContext(inputs=run.inputs, outputs=outputs,
                          run_id=run_id, workflow_id=self.wf.id)
        self._persist(run, ctx, waiting_node="")
        self._emit({"event": "approved", "run_id": run_id, "node": waiting})
        merged = dict(run.inputs or {})
        merged.update(inputs or {})          # ★ 调用方重新注入的运行时对象覆盖落盘值
        return self.run(inputs=merged, run_id=run_id, resume=True)

    # ---- 调度 ----
    def _ready_batch(self, pending: list[Node], run: Run) -> list[Node]:
        """入度已满足的节点集合。依赖必须**全部是 DONE** 或**可选且已跳过**。"""
        batch: list[Node] = []
        for n in pending:
            if all(self._dep_satisfied(dep, run) for dep in n.needs):
                batch.append(n)
        return batch

    def _dep_satisfied(self, dep_id: str, run: Run) -> bool:
        s = run.steps.get(dep_id)
        if s is None:
            return False
        if s.state == StepState.DONE:
            return True
        # 可选节点失败/跳过 → 下游仍可继续（但它拿不到产出）
        node = self._nodes.get(dep_id)
        if (node is not None and node.optional
                and s.state in (StepState.FAILED, StepState.BLOCKED)):
            return True
        return False

    def _run_batch(self, batch: list[Node], run: Run, ctx: NodeContext) -> None:
        """执行一批。★ 串行类型走专用锁，其余走线程池并发。"""
        serial = [n for n in batch if n.type in SERIAL_TYPES]
        parallel = [n for n in batch if n.type not in SERIAL_TYPES]

        if parallel:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures: dict[Future, Node] = {}
                for n in parallel:
                    self._mark_running(n, run)
                    futures[pool.submit(self._execute_with_retry, n, run, ctx)] = n
                for fut in futures:
                    node = futures[fut]
                    try:
                        outcome = fut.result()
                    except BaseException as e:               # noqa: BLE001
                        outcome = NodeOutcome(ok=False, error=f"{type(e).__name__}: {e}")
                    self._apply(node, outcome=outcome, run=run, ctx=ctx)

        # 串行：一次一个，且持有全局锁（matplotlib 不是线程安全的）
        for n in serial:
            self._mark_running(n, run)
            with _SERIAL_LOCK:
                outcome = self._execute_with_retry(n, run, ctx)
            self._apply(n, outcome=outcome, run=run, ctx=ctx)

    # ---- 单节点执行（含重试与超时）----
    def _execute_with_retry(self, node: Node, run: Run, ctx: NodeContext) -> NodeOutcome:
        fn = executor_for(node.type)
        if fn is None:
            return NodeOutcome(ok=False, error=f"没有可执行 {node.type} 的执行器")

        attempts = node.effective_retry + 1                 # ★ 非幂等节点这里是 1
        last: NodeOutcome | None = None
        for i in range(attempts):
            step = run.step(node.id)
            step.attempts += 1
            try:
                outcome = self._call_with_timeout(fn, node, ctx, node.timeout_s)
            except NotSupportedError as e:
                return NodeOutcome(ok=False, error=str(e))
            except BaseException as e:                       # noqa: BLE001
                outcome = NodeOutcome(ok=False, error=f"{type(e).__name__}: {e}")
            last = outcome
            if outcome.ok:
                return outcome
            if i + 1 < attempts:
                time.sleep(BACKOFF_BASE * (2 ** i))          # 指数退避
                self._emit({"event": "retry", "node": node.id,
                            "attempt": i + 1, "attempts": attempts,
                            "error": outcome.error})
        return last or NodeOutcome(ok=False, error="未知失败")

    def _call_with_timeout(
        self, fn: Callable[[Node, NodeContext], NodeOutcome],
        node: Node, ctx: NodeContext, timeout_s: float | None,
    ) -> NodeOutcome:
        """带超时地调用执行函数。

        ★ Python 没法强杀线程，所以超时后**只能放弃等待**并如实标记超时 ——
        绝不假装它成功了。真正的清理留给节点自己（节点应自己按 timeout 配置）。
        """
        if not timeout_s:
            return fn(node, ctx)

        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["ok"] = fn(node, ctx)
            except BaseException as e:                       # noqa: BLE001
                box["err"] = e

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            return NodeOutcome(
                ok=False, error=f"节点 {node.id} 超时（{timeout_s}s）")
        if "err" in box:
            raise box["err"]
        return box.get("ok") or NodeOutcome(ok=False, error="执行器没有返回结果")

    # ---- 状态应用 ----
    def _mark_running(self, node: Node, run: Run) -> None:
        step = run.step(node.id)
        step.transition(StepState.RUNNING)
        step.started_at = datetime.now().isoformat(timespec="milliseconds")
        self._emit({"event": "node_start", "node": node.id, "type": node.type})

    def _apply(self, node: Node, *, outcome: NodeOutcome,
               run: Run, ctx: NodeContext) -> None:
        step = run.step(node.id)
        step.ended_at = datetime.now().isoformat(timespec="milliseconds")

        # ★ 人工确认：状态置 WAITING，**不完成也不失败** —— 调度循环会据此暂停
        if outcome.waiting:
            step.output = outcome.output
            step.transition(StepState.WAITING)
            self._emit({"event": "node_waiting", "node": node.id})
            return

        if outcome.ok:
            step.output = outcome.output
            ctx.outputs[node.id] = outcome.output
            step.transition(StepState.DONE)
            self._emit({"event": "node_done", "node": node.id})
            return

        step.error = outcome.error
        policy = (node.on_fail or "fail_fast").strip()
        if policy not in ON_FAIL_POLICIES:
            step.transition(StepState.FAILED)
            step.error += f"（未知失败策略 {policy!r}，按 fail_fast 处理）"
            return
        if policy in ("degrade", "skip_if_optional") and node.optional:
            # 可选节点失败 → 标记 FAILED，但引擎继续（下游按"可选已跳过"处理）
            step.transition(StepState.FAILED)
            return
        if policy == "degrade":
            step.transition(StepState.FAILED)
            return
        step.transition(StepState.FAILED)

    def _should_abort(self, run: Run) -> bool:
        """该不该整体终止？

        ★ **先看 `optional` 再看策略** —— 可选节点的失败**永远不该终止整体**
        （这正是"可选"的含义）。第一版没判 optional，导致可选节点失败把整轮拖死。
        """
        for s in run.steps.values():
            if s.state not in (StepState.FAILED, StepState.BLOCKED):
                continue
            node = self._nodes.get(s.node_id)
            if node is None:
                continue
            if node.optional:
                continue                                     # ★ 可选节点失败不终止
            policy = (node.on_fail or "fail_fast").strip()
            if policy not in ON_FAIL_POLICIES:
                return True                                  # 未知策略 → 保守终止
            if policy == "fail_fast":
                return True
        return False

    def _final_state(self, run: Run) -> StepState:
        """整体状态。

        ★ 判定顺序（**先熔断、再非可选失败、最后才看是否全成功**）：
          ① 有任何 `blocked` → `blocked`
          ② 有**非可选**节点 `failed` → `failed`
          ③ 没有剩余未执行的节点 → `done`（可选节点的失败不算整体失败）
          ④ 否则 → `failed`（有节点卡在 pending/running，说明有依赖没满足）
        """
        states = [s.state for s in run.steps.values()]
        if not states:
            return StepState.FAILED
        if any(st == StepState.BLOCKED for st in states):
            return StepState.BLOCKED

        for s in run.steps.values():
            if s.state != StepState.FAILED:
                continue
            node = self._nodes.get(s.node_id)
            if node is not None and node.optional:
                continue                                     # ★ 可选失败不算整体失败
            return StepState.FAILED

        # 到这里：没有非可选失败、也没有熔断
        if all(st in (StepState.DONE, StepState.FAILED) for st in states):
            return StepState.DONE
        return StepState.FAILED

    # ---- 持久化与事件 ----
    def _persist(self, run: Run, ctx: NodeContext, *, waiting_node: str = "") -> None:
        if not self.persist:
            return
        try:
            store.save_run(run)
            store.save_checkpoint(Checkpoint(
                run_id=run.run_id,
                workflow_id=run.workflow_id,
                node_outputs=dict(ctx.outputs),
                node_states={k: v.state.value for k, v in run.steps.items()},
                waiting_node=waiting_node or self._waiting_node(run),
            ))
        except Exception as e:                               # noqa: BLE001
            # ★ 落盘失败不该让已经跑完的工作流失败 —— 但要**留下痕迹**。
            # 捕获范围从 OSError 放宽到 Exception：实测过不可序列化的注入对象
            # 会抛 TypeError，把整个运行带崩（守卫抓到的真 bug）。
            self._emit({"event": "persist_failed", "run_id": run.run_id,
                        "error": f"{type(e).__name__}: {e}"})

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(payload)
        except BaseException:                                # noqa: BLE001
            pass


def run_workflow(
    workflow: Workflow,
    inputs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Run:
    """便捷入口：`Engine(workflow).run(inputs)`。"""
    return Engine(workflow, **kwargs).run(inputs)


__all__ = [
    "BACKOFF_BASE", "DEFAULT_MAX_WORKERS", "Engine", "EngineError",
    "NodeContext", "NodeOutcome", "NotSupportedError", "ON_FAIL_POLICIES",
    "SERIAL_TYPES", "executor_for", "run_workflow",
]