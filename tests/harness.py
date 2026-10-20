# -*- coding: utf-8 -*-
"""脚本化 LLM 夹具 —— 让 Agent 循环**零 API 成本**可测。

【为什么需要】`check_skill_wiring.py` 早已手工 stub 过 `chat_with_tools`，
说明这个接缝**已经存在**，只是每次都要重写一遍。本模块把它固化成可复用夹具。

【⚠️ 这套夹具能测什么、不能测什么 —— 必须说清楚】

  能测（**机制/管线**）：
    · 工具被调用后，结果是否真的回灌进消息、循环是否继续/收敛
    · 分级网关的档位是否被遵守（L0 时**工具循环一次都不跑**）
    · 能力闸门是否真的拦住（且挡住时工具实现**没跑**）
    · 预算层是否截断、审计行是否产出
    · 工具返回错误时，轨迹是否**不进结晶档**（G2 门禁）

  不能测（**模型选择**）：
    · "模型会不会挑对工具" —— 那是脚本决定的，用它来断言工具选择是**循环论证**。
      真实工具选择由付费评测覆盖（`eval_tools.py` / `eval_skill_acceptance.py`）。

  所以本夹具的场景断言对象是**产物与机制触发**，而不是"答案对不对"。
  这也正是 `docs/ARCHITECTURE.md` §8.9 与 `docs/ABSORPTION.md` §8.4 反复强调的方向。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from core.rag_engine import RAGEngine          # noqa: E402
from core.query_router import classify         # noqa: E402


# ---- OpenAI 风格的假响应对象（只带 `_execute_tool_loop` 用到的字段）----


@dataclass
class FakeFunction:
    name: str
    arguments: str          # JSON 字符串（与真实 API 一致）


@dataclass
class FakeToolCall:
    function: FakeFunction
    # ⚠️ `id` 是必需的：`_execute_tool_loop` 用它写 tool 消息的 `tool_call_id`。
    # OpenAI/DeepSeek 的每个 tool_call 都带唯一 id，缺了会让回灌消息不合法。
    id: str = "call_fake"
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] = field(default_factory=list)


@dataclass
class RunResult:
    """一次脚本化问答的可断言产物。"""

    question: str
    answer: str
    route: str                       # L0 / L1 / L2（与引擎同源判定）
    tools_called: list[str] = field(default_factory=list)   # 实际执行的工具（按序）
    refused: list[str] = field(default_factory=list)        # 被能力闸门拒绝的工具
    tool_loop_rounds: int = 0        # chat_with_tools 被调次数（0 = 完全没进工具循环）
    kb_calls: int = 0                # 知识库检索次数（0 = 跳过了）
    l4_calls: int = 0                # L4 记忆召回次数（0 = 跳过了）
    crystallized_added: list[str] = field(default_factory=list)   # 本轮**真正新增**的结晶档条目 id
    last_messages: list[dict] = field(default_factory=list)       # 末次请求的消息（可断言回灌）

    def fed_back(self, tool_name: str) -> bool:
        """工具结果是否被**回灌进消息**（证明循环真的把结果交给了模型）。

        按 `tool_call_id` 前缀匹配 —— 本夹具故意把工具名编进 id（`call_<工具名>_<序号>`），
        因为真实链路里 tool 消息**不带 `name` 字段**，只有 `tool_call_id` 可对应。

        注意：循环把 assistant 消息以**对象**形式追加进 messages，所以这里必须
        容忍非 dict 项（写这段时正是漏了这点，被自己的评测当场抓住）。
        """
        prefix = f"call_{tool_name}"
        for m in self.last_messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool" and str(m.get("tool_call_id", "")).startswith(prefix):
                return True
        return False


class Harness:
    """脚本化引擎夹具（**默认不拥有用户数据** = 不碰生产目录）。"""

    def __init__(self, *, owns_user_data: bool = False,
                 sandbox_name: str = "harness_sandbox",
                 kb_context: str = "", l4_facts: list[str] | None = None,
                 l3_summary: str = "") -> None:
        self.sandbox = ROOT / "tests" / "eval_outputs" / sandbox_name
        if self.sandbox.exists():
            shutil.rmtree(self.sandbox, ignore_errors=True)
        self.sandbox.mkdir(parents=True, exist_ok=True)

        self.engine = RAGEngine(
            user_data_dir=None if not owns_user_data else self.sandbox,
            owns_user_data=owns_user_data,
        )
        self._kb_context = kb_context
        self._l4_facts = l4_facts or []
        self._l3_summary = l3_summary
        self._counters: dict[str, int] = {}
        self._queue: list[list[tuple[str, dict]]] = []
        self._answer = "这是脚本化的最终回答。"
        self._last_messages: list[dict] = []
        self._patch()

    # ---- 打桩 ----

    def _patch(self) -> None:
        e = self.engine
        e._rewrite_query = lambda q: q                                   # type: ignore
        e._generate_title = lambda *a, **k: None                         # type: ignore
        e._start_memory_worker = lambda *a, **k: None                    # type: ignore

        kb = self._kb_context
        l4 = self._l4_facts
        l3 = self._l3_summary

        def _kb(*a, **k):
            self._counters["kb"] = self._counters.get("kb", 0) + 1
            return kb

        def _l4(*a, **k):
            self._counters["l4"] = self._counters.get("l4", 0) + 1
            return list(l4)

        e.kb.retrieve_context = _kb                                       # type: ignore
        e.memory.retrieve_l4_memory = _l4                                 # type: ignore
        e.memory.retrieve_session_summaries = lambda *a, **k: []          # type: ignore

        # 会话摘要（供 L3 段落有内容可注入）
        if l3:
            sess = e.create_session()
            sess.summary_memory = l3
        else:
            e.create_session()

        def _chat_with_tools(messages, tools, model_name=None):
            self._counters["loop"] = self._counters.get("loop", 0) + 1
            self._last_messages = list(messages)
            if not self._queue:
                return FakeMessage(content="（无更多工具调用）", tool_calls=[])
            turn = self._queue.pop(0)
            return FakeMessage(content=None, tool_calls=[
                FakeToolCall(
                    function=FakeFunction(n, json.dumps(a, ensure_ascii=False)),
                    # 工具名编进 id 前缀：真实链路的 tool 消息**不带 name 字段**
                    # （只有 role/tool_call_id/content），所以只能靠 id 对应回工具名。
                    id=f"call_{n}_{i}",
                )
                for i, (n, a) in enumerate(turn)
            ])

        def _stream(messages, model_name=None, enable_thinking=False,
                    reasoning_effort=None):
            # 流式这一路**总会跑**（L0/L1 也会），所以在这里也记一次消息 ——
            # 否则"跳过了工具循环"的场景拿不到任何消息，无法断言组装结果。
            self._last_messages = list(messages)
            yield ("content", self._answer)

        e.llm.chat_with_tools = _chat_with_tools                          # type: ignore
        e.llm.stream_response = _stream                                   # type: ignore

    # ---- 运行 ----

    def run(self, question: str, *,
            tool_turns: list[list[tuple[str, dict]]] | None = None,
            answer: str | None = None) -> RunResult:
        """跑一轮脚本化问答，收集可断言产物。"""
        self._queue = [list(t) for t in (tool_turns or [])]
        self._answer = answer if answer is not None else "这是脚本化的最终回答。"
        self._counters = {}
        self._last_messages = []

        called: list[str] = []
        refused: list[str] = []
        added: list[str] = []

        original_exec = self.engine._execute_tool
        original_rec = self.engine.crystallized.record_trajectory

        def _exec(name, args):
            out = original_exec(name, args)
            if "能力闸门拒绝" in out:
                refused.append(name)
            else:
                called.append(name)
            return out

        def _rec(*a, **k):
            r = original_rec(*a, **k)
            # 只记**真正新增**的（action == "added"）。
            # 被门禁拒绝的（tool_error / 非空链 / 容量淘汰…）不进这个列表 ——
            # 否则"被拒轨迹不进结晶档"这条断言永远为假（写这段时踩过）。
            if getattr(r, "action", None) == "added":
                added.append(str(getattr(r, "entry_id", "added")))
            return r

        self.engine._execute_tool = _exec                            # type: ignore
        self.engine.crystallized.record_trajectory = _rec            # type: ignore

        route = classify(question).level
        answer_text = ""
        try:
            for mtype, chunk in self.engine.ask(question):
                if mtype == "content":
                    answer_text += chunk
        finally:
            self.engine._execute_tool = original_exec                # type: ignore
            self.engine.crystallized.record_trajectory = original_rec  # type: ignore

        return RunResult(
            question=question,
            answer=answer_text,
            route=route,
            tools_called=called,
            refused=refused,
            tool_loop_rounds=self._counters.get("loop", 0),
            kb_calls=self._counters.get("kb", 0),
            l4_calls=self._counters.get("l4", 0),
            crystallized_added=added,
            last_messages=self._last_messages,
        )

    def close(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)


def any_json() -> dict:
    """占位参数（工具参数内容不影响机制断言）。"""
    return {}