# -*- coding: utf-8 -*-
"""主链路 → 编排层的**移交**（`docs/HANDOFF.md` 的实现）。

【它解决什么】
    主链路的工具循环只有 `max_loops = 3` 轮（`core/rag_engine.py`）。
    **依赖链**（A 的结果决定 B 查什么）每层要 1 轮，3 层就撞墙。
    撞墙后**在此之前是静默的** —— 模型拿着不完整的中间结果，
    正常组织出一段"看起来完整"的答案，用户不知道链条断了。

【本模块做三件事】
    ① **判定**：要不要移交（`should_handoff`）—— 只看**观测到的事实**，不预测
    ② **携带**：把已经跑过的调用打包成工作流输入（`build_inputs`）——
       那几轮**已经花掉了**，编排层从这些结果继续，不重头再查
    ③ **收尾**：把"这部分是部分结果"如实告诉用户（`partial_notice`）

【★ 刻意不做的事】
    · **不猜"还剩哪些目标要查"** —— 依赖链的形状取决于中间结果，
      "明天有雨 → 要改查土壤湿度"这件事是查完才知道的。事前算不出来。
    · **不声称能自动补完** —— 移交的前提是"有一个可执行的工作流"。
      没有的话，诚实地告诉用户"链条没走完"，比假装完整强。

【与既有代码的关系】
    · `core/orchestration/`：编排层本体（校验 / 执行 / 续跑 / 签字），本模块**调用**它
    · `core/tools/dispatch.py::LoopGuard`：提供 `wants_more` / `completed_calls()`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# ① 判定
# ---------------------------------------------------------------------------


def should_handoff(*, wants_more: bool) -> tuple[bool, str]:
    """要不要把这次请求移交给编排层。

    ★ **只看一个观测到的事实**：工具循环轮次用尽、而模型**仍然要求**调用工具。

    为什么不需要别的判据：
      · "目标数量多" → **已实测证伪**（14 个地区 1 轮就并发查完了）；
      · "出现批处理词" → 词面判定，本项目已栽过两次（`is_refusal` 假阳性、
        空转判定误杀真回答），不能用；
      · "依赖链深度" → **事前算不出来**（取决于中间结果，查完才知道）。

    所以移交判据只有这一条，而且它是**事实**不是阈值 —— 观测不会误判。
    """
    if wants_more:
        return True, ("工具循环轮次用尽，而模型仍要求调用工具 —— "
                      "依赖链没走完，对话模式已无法继续")
    return False, "本轮正常结束（模型不再需要工具）"


# ---------------------------------------------------------------------------
# ② 携带
# ---------------------------------------------------------------------------


def _short(text: Any, limit: int = 400) -> str:
    """把工具结果压到可落盘的长度（保留尾部 —— 关键数字常在后面）。"""
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[: limit // 2] + "\n…（中略）…\n" + s[-limit // 2:]


def build_inputs(completed: list[dict], *, question: str = "",
                 rounds_used: int = 0) -> dict:
    """把**已经跑过**的调用打包成工作流输入。

    ★ 移交时**必须带上它**：那几轮已经花掉了（LLM 调用 + 工具执行），
      编排层应当从这些结果继续，而不是重头再查一遍。

    ★ 返回的结构是**纯 JSON 可序列化**的 —— 编排层要把它写进断点文件
      （`core/orchestration/store.py` 用 `json.dumps`）。
      工具结果可能是 `ToolResult` 之类的对象，所以统一转成字符串。
    """
    done: list[dict] = []
    chain: list[str] = []
    for item in completed or []:
        name = str((item or {}).get("name") or "")
        args = (item or {}).get("args")
        chain.append(name)
        done.append({
            "name": name,
            # 参数原样保留（能 json 就 json，不能就转字符串）
            "args": _jsonable(args),
            "result": _short((item or {}).get("result")),
        })
    return {
        "question": str(question or ""),
        "rounds_used": int(rounds_used or 0),
        "done_count": len(done),
        # 供流程里 `{{done}}` 之类引用；也方便人读
        "done": done,
        # 工具调用链（如 ["get_weather", "query_cotton_stats"]）——
        # 技能库登记用的就是这个形状
        "tool_chain": chain,
    }


def _jsonable(v: Any) -> Any:
    """尽力把参数转成 JSON 可序列化结构；失败就转字符串。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    try:
        import json
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# ③ 收尾：如实告诉用户"这是部分结果"
# ---------------------------------------------------------------------------

#: 部分结果提示的**固定开头** —— 守卫与前端可以据此识别。
PARTIAL_MARKER = "〔部分结果〕"


def partial_notice(inputs: dict, *, handoff_ok: bool = False,
                   handoff_msg: str = "") -> str:
    """生成"这是部分结果"的提示文本。

    ★ **必须说清楚三件事**，否则用户仍会以为答案是完整的：
      ① 这不是全部；② 已完成到哪一步；③ 接下来能怎么办。

    ★ 措辞不能甩锅给用户（"请重新提问"），而要说明**系统这一侧**的局限。
    """
    n = int(inputs.get("done_count") or 0)
    rounds = int(inputs.get("rounds_used") or 0)
    steps = "、".join(inputs.get("tool_chain") or []) or "（无）"

    lines = [
        "",
        "---",
        f"{PARTIAL_MARKER}以上内容基于**已完成的前 {n} 步**"
        f"（用了 {rounds} 轮工具调用：{steps}）。",
        "",
        "**这个任务没有走完**：模型在最后一轮仍然需要继续调用工具，"
        "但对话模式的轮次已用尽。所以**上面可能是局部的结论**，"
        "请不要当成完整的分析结果。",
    ]
    if handoff_ok:
        lines.append("")
        lines.append("已尝试转入**任务模式**继续执行"
                     + (f"（{handoff_msg}）" if handoff_msg else "") + "。")
    else:
        lines += [
            "",
            "**你可以**：",
            "  · 把问题**拆小**（比如一次问一两个地区 / 一个环节），我就能一次答完；",
            "  · 或者告诉我需要按顺序做的**完整步骤**，我用任务模式逐段执行。",
        ]
        if handoff_msg:
            lines += ["", f"（任务模式未能启动：{handoff_msg}）"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


@dataclass
class HandoffDecision:
    """一次移交判定的完整结果（便于记录与测试）。"""

    needed: bool
    reason: str
    inputs: dict = field(default_factory=dict)

    def __str__(self) -> str:                                 # noqa: D105
        return f"{'移交' if self.needed else '不移交'}: {self.reason}"


def decide(*, wants_more: bool, completed: list[dict] | None = None,
           question: str = "", rounds_used: int = 0) -> HandoffDecision:
    """一站式：判定 + 打包输入。**纯函数**，不碰网络、不碰磁盘。"""
    needed, reason = should_handoff(wants_more=wants_more)
    if not needed:
        return HandoffDecision(False, reason, {})
    return HandoffDecision(
        True, reason,
        build_inputs(completed or [], question=question,
                     rounds_used=rounds_used),
    )


# ---------------------------------------------------------------------------
# ④ 移交工作流：生成 → 静态校验 → 回退（HANDOFF 第 4 步）
# ---------------------------------------------------------------------------

#: 移交后在编排层执行的工作流 id
RESUME_WORKFLOW_ID = "handoff_resume"


def build_resume_yaml(inputs: dict) -> str:
    """生成移交用的工作流 YAML。

    【它为什么长这样 —— 这是本设计里最需要解释的一个取舍】

    移交时**我们并不知道"剩下的活是什么"**：
    依赖链的形状取决于中间结果（"明天有雨 → 才要改查土壤湿度"），
    那是查完才知道的，事前算不出来。所以一个"自动把任务补完"的工作流
    **做不出来** —— 硬写一个只会是假装。

    那移交能做什么？**做它真正能做的事**：

      1. **固化现场**：把已完成的结果和工作流输入一起落盘（`store.save_run`），
         这样它不会随对话结束而丢失；
      2. **请求人工确认**：链条断了是**需要人介入**的信号
         （农业决策不可逆 —— 提前采收的损失收不回来）。
         这正是 `human.confirm` 节点的用途，而且它是**零 LLM 成本**的。

    ★ 所以本工作流**没有 llm 节点**：它不猜、不编、不花钱，
      只做"记录 + 请人决定"这两件确实能做好的事。
    """
    q = str(inputs.get("question") or "").replace('"', "'")[:200]
    n = int(inputs.get("done_count") or 0)
    rounds = int(inputs.get("rounds_used") or 0)
    chain = "、".join(inputs.get("tool_chain") or []) or "（无）"

    prompt = (
        f"上一个对话任务**没有走完**：原始问题「{q}」，"
        f"已完成 {n} 步（用了 {rounds} 轮工具调用：{chain}），"
        f"模型仍需要继续但对话轮次已用尽。\n"
        f"已完成的部分结果已保存在本次运行的输入里。\n"
        f"请确认：**是否继续处理这个任务？**（拒绝则回到对话模式，"
        f"由用户把问题拆小后重新提问）"
    ).replace("\n", "\n        ")

    return f"""# 由 core/handoff.py 自动生成 —— 移交续跑工作流
# ★ 本文件由程序生成，**不建议手工编辑**（改了会被静态校验与守卫发现）。
# ★ 它不猜"剩下的活是什么" —— 那取决于中间结果，事前算不出来。
#   它做两件确实能做好的事：固化现场 + 请人确认（见 build_resume_yaml 的注释）。
id: {RESUME_WORKFLOW_ID}
version: 1
description: 移交 —— 链条未走完，固化现场并请人确认下一步
inputs:
  question: {{ type: string, required: true }}
  done: {{ type: array, required: false }}
  done_count: {{ type: integer, required: true }}
  rounds_used: {{ type: integer, required: false }}
  tool_chain: {{ type: array, required: false }}
  out_dir: {{ type: string, required: false }}
nodes:
  # ① 请人确认：链条断了是**需要人介入**的信号（农业决策不可逆）
  - id: confirm
    type: human.confirm
    params:
      prompt: "{prompt}"
      max_reject: 1
    side_effect: none

  # ② ★ 存档：把已完成的中间结果落成一份 Markdown
  #    —— 让"部分结果"变成用户**能带走**的文件，而不是只留在聊天记录里
  - id: archive
    type: report.partial
    params:
      question: "{{{{question}}}}"
      done: "{{{{done}}}}"
      rounds_used: "{{{{rounds_used}}}}"
      tool_chain: "{{{{tool_chain}}}}"
      out_dir: "{{{{out_dir}}}}"
    side_effect: disk
    needs: [confirm]
"""


@dataclass
class HandoffOutcome:
    """一次移交尝试的完整结果。"""

    ok: bool
    #: 是否走到了执行阶段（False = 在生成/校验阶段就被挡下）
    executed: bool = False
    run_id: str = ""
    error: str = ""
    issues: list[str] = field(default_factory=list)

    def message(self) -> str:
        if self.ok:
            return f"已转入任务模式（run_id={self.run_id}），等待确认"
        return self.error or "移交失败"


def validate_workflow_text(yaml_text: str) -> tuple[object | None, list[str]]:
    """解析 + 静态校验一段 YAML。

    返回 `(workflow, 问题列表)`。**问题列表非空即视为不合格**（Fail-Closed）。

    ★ 为什么这一步不能省：LLM 生成的流程可能参数错、依赖成环、
      引用了不存在的节点 —— `validator` 里 10 类错误各有反例，
      它就是为这一刻准备的。
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

    msgs = [validator.format_issues([i]) if hasattr(validator, "format_issues")
            else str(i) for i in issues]
    # format_issues 对单条可能返回空串，兜底用 str
    msgs = [m or str(i) for m, i in zip(msgs, issues)]
    return wf, msgs


def prepare(inputs: dict) -> HandoffOutcome:
    """生成 + 校验，**不执行**。

    失败时返回 `ok=False`，调用方据此**回退到对话模式**并如实告知用户。
    """
    try:
        yaml_text = build_resume_yaml(inputs)
    except Exception as e:                                   # noqa: BLE001
        return HandoffOutcome(False, error=f"生成工作流失败：{type(e).__name__}: {e}")

    wf, issues = validate_workflow_text(yaml_text)
    if wf is None or issues:
        return HandoffOutcome(
            False, issues=issues,
            error="生成的工作流未通过静态校验：" + "；".join(issues[:3]))

    return HandoffOutcome(True)


__all__ = [
    "PARTIAL_MARKER", "RESUME_WORKFLOW_ID", "HandoffDecision",
    "HandoffOutcome", "build_inputs", "build_resume_yaml", "decide",
    "execute", "get_runner", "partial_notice", "prepare",
    "render_partial_archive", "set_runner", "should_handoff",
    "validate_workflow_text",
]

# ---------------------------------------------------------------------------
# ⑤ ★ 真执行（HANDOFF 第 3 步的"执行"那半边）
# ---------------------------------------------------------------------------

#: 进程内共享的执行器。**懒创建**，可被测试/UI 注入替换。
_RUNNER: Any = None


def get_runner() -> Any:
    """取共享的 `TaskRunner`（懒创建）。"""
    global _RUNNER
    if _RUNNER is None:
        from core.task_runner import TaskRunner
        _RUNNER = TaskRunner()
    return _RUNNER


def set_runner(runner: Any) -> None:
    """注入/替换执行器（测试与 UI 用）。传 `None` 恢复懒创建。"""
    global _RUNNER
    _RUNNER = runner


def execute(inputs: dict, *, runner: Any = None) -> HandoffOutcome:
    """生成 → 静态校验 → **真在后台执行**。

    ★ 与 `prepare()` 的区别：这个会**真的启动工作流**（后台线程），
      所以 `HandoffOutcome.executed` 与 `run_id` 会被填上。

    ★ 为什么后台执行而不是就地跑：`ask()` 是**流式生成器**，边跑边 `yield`。
      若在里面同步跑工作流，界面会一直转圈、一句反馈都没有
      （`docs/PERCEPTION.md` §5.2 记的就是这件事）。

    ★ 校验不过时**不启动线程** —— 不留一个"看起来在跑"的空任务。
    """
    outcome = prepare(inputs)
    if not outcome.ok:
        return outcome

    try:
        yaml_text = build_resume_yaml(inputs)
        r = runner if runner is not None else get_runner()
        handle = r.submit_yaml(yaml_text, inputs)
    except Exception as e:                                   # noqa: BLE001
        outcome.ok = False
        outcome.error = f"启动工作流失败：{type(e).__name__}: {e}"
        return outcome

    outcome.executed = bool(getattr(handle, "launched", False))
    outcome.run_id = str(getattr(handle, "run_id", "") or "")
    if not outcome.executed:
        issues = list(getattr(handle, "issues", []) or [])
        outcome.ok = False
        outcome.error = "工作流未启动：" + ("；".join(issues[:3]) or "未知原因")
    return outcome


# ---------------------------------------------------------------------------
# ⑥ 部分结果存档（工作流里 `report.partial` 节点用）
# ---------------------------------------------------------------------------


def render_partial_archive(inputs: dict) -> str:
    """把"已完成的部分结果"渲染成一份可存档的 Markdown。

    ★ 为什么要有它：移交后的工作流若只停在"请确认"，
      用户手上就**没有任何可带走的东西**。这份存档让"部分结果"变成
      一个可分享、可追溯的文件，而不是只留在聊天记录里。
    """
    import json as _json

    question = str(inputs.get("question") or "（未记录原始问题）")
    try:
        rounds = int(inputs.get("rounds_used") or 0)
    except (TypeError, ValueError):
        rounds = 0
    done = list(inputs.get("done") or [])
    chain = list(inputs.get("tool_chain") or [])

    lines: list[str] = []
    add = lines.append
    add("# 部分结果存档")
    add("")
    add("> 本文件由**移交机制**自动生成 —— 当对话模式的工具循环轮次用尽、"
        "而任务尚未走完时，系统把已完成的中间结果固化成这份存档。")
    add("")
    add(f"- **原始问题**：{question}")
    add(f"- **已完成步数**：{len(done)}")
    add(f"- **消耗轮数**：{rounds}")
    add(f"- **工具链**：{' → '.join(str(c) for c in chain) if chain else '（无）'}")
    add("")
    add("---")
    add("")
    add("## 已完成的步骤")
    add("")
    if not done:
        add("（没有可记录的步骤）")
    for i, item in enumerate(done, 1):
        name = str((item or {}).get("name") or "?")
        args = (item or {}).get("args")
        result = str((item or {}).get("result") or "")
        add(f"### {i}. `{name}`")
        add("")
        if args not in (None, {}, ""):
            add("**参数**")
            add("")
            add("```json")
            try:
                add(_json.dumps(args, ensure_ascii=False, indent=2))
            except (TypeError, ValueError):
                add(str(args))
            add("```")
            add("")
        add("**结果**")
        add("")
        add("```text")
        add(result.rstrip() or "（空）")
        add("```")
        add("")
    add("---")
    add("")
    add("## 边界声明（必读）")
    add("")
    add("1. **这不是完整结果。** 任务在工具循环轮次用尽时被中断，"
        "上表只覆盖已完成的部分。")
    add("2. **不要把它当作完整分析的结论。** 缺失的部分可能改变判断。")
    add("3. 若要继续，请把问题**拆小**（一次问一两个目标），"
        "或给出完整的步骤让任务模式逐段执行。")
    add("")
    return "\n".join(lines)
