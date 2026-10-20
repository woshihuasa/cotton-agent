# -*- coding: utf-8 -*-
"""技能库接线守卫 —— 验证 `rag_engine` 的四处接线**真的生效**。

【定位】`check_procedural_memory.py` 验证技能库**自身**（门禁、检索、持久化），
        但它刻意零第三方依赖，因此**不 import `rag_engine`**，看不到接线。
        本脚本补这一层：把 `RAGEngine` 真正构造出来，跑通一轮 `ask()`。

【为什么必须单独一个守卫】语法通过、导入通过，都不等于接线生效。
        本项目已有的评测方法缺陷 M4（API 失败被当成回答）说明：
        **"看起来接上了"与"实际按设计工作"是两件事**。

【零 LLM / 零网络】全部外部调用以桩件替换：
        · 查询改写 → 原样返回（省一次 LLM）
        · 知识库检索 / L4 / L3 检索 → 固定空值（省 embedding）
        · `chat_with_tools` / `stream_response` → 固定桩
        · 工具执行 → 固定 JSON（真实工具路径由 `smoke_mcp` / `eval_numeric` 覆盖）
        · 标题生成 / L4 记忆线程 → 置空（避免后台真调 LLM）

【沙箱】`tests/eval_outputs/skill_wiring_sandbox/`（已 gitignore），**不触碰生产路径**。

【用法】python tests/check_skill_wiring.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.rag_engine import RAGEngine          # noqa: E402
from core.skill_loader import MAX_SKILL_SECTION_CHARS   # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "skill_wiring_sandbox"
SKILL_FILE = SANDBOX / "procedural_memory" / "index.jsonl"


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        mark = "OK  " if ok else "FAIL"
        line = f"  [{mark}] {desc}"
        if detail and not ok:
            line += f"   ← {detail}"
        print(line)
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


# ---------------------------------------------------------------------------
# 桩件：模拟 OpenAI 风格的响应对象（只带 `_execute_tool_loop` 用到的字段）
# ---------------------------------------------------------------------------

class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, tool_calls=None, content: str = "") -> None:
        self.tool_calls = tool_calls or []
        self.content = content


def _make_engine() -> RAGEngine:
    """构造沙箱引擎，并把一切外部调用替换为桩件。"""
    engine = RAGEngine(user_data_dir=SANDBOX)

    # 省 LLM / embedding：这些都在生产链路里，但与本守卫要验证的接线无关
    engine._rewrite_query = lambda q: q                              # type: ignore[method-assign]
    engine.kb.retrieve_context = lambda *a, **k: ""                  # type: ignore[method-assign]
    engine.memory.retrieve_l4_memory = lambda *a, **k: []            # type: ignore[method-assign]
    engine.memory.retrieve_session_summaries = lambda *a, **k: []    # type: ignore[method-assign]

    # 后台线程与标题生成都会真调 LLM，一并置空
    engine._generate_title = lambda *a, **k: None                    # type: ignore[method-assign]
    engine._start_memory_worker = lambda *a, **k: None               # type: ignore[method-assign]

    # 工具执行走桩：真实工具路径由 smoke_mcp / eval_numeric 覆盖
    engine._execute_tool = lambda name, args: json.dumps(          # type: ignore[method-assign]
        {"ok": True, "tool": name}, ensure_ascii=False
    )

    # 与生产一致：`_build_messages` 依赖"当前会话"存在（这是改动前就有的前提，
    # 生产里会话由界面创建）。沙箱从零开始，故显式建一个。
    engine.create_session()
    return engine


def _install_fake_llm(engine: RAGEngine, answer: str = "这是最终回答。") -> dict:
    """装上一个"先调工具、再回答"的假 LLM，返回可读的调用计数。"""
    calls = {"chat": 0, "stream": 0}

    def chat_with_tools(messages, tools, model_name=None):
        calls["chat"] += 1
        if calls["chat"] == 1:
            # 第一轮：要求调用两个工具（含参数值，用于验证"只记参数名"）
            return _FakeMessage(tool_calls=[
                _FakeToolCall("call_1", "get_weather",
                              json.dumps({"location": "阿拉尔", "date": "2026-10-15"})),
                _FakeToolCall("call_2", "query_cotton_stats",
                              json.dumps({"region": "阿克苏", "year": 2022})),
            ])
        # 第二轮：不再要求工具 → 循环正常结束
        return _FakeMessage(content=answer)

    def stream_response(messages, model_name=None, enable_thinking=False,
                            reasoning_effort=None):
        calls["stream"] += 1
        yield ("content", answer)

    engine.llm.chat_with_tools = chat_with_tools      # type: ignore[method-assign]
    engine.llm.stream_response = stream_response      # type: ignore[method-assign]
    return calls


def main() -> int:
    c = Checker()
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    c.section("第 1 部分：作用域隔离（技能库落在沙箱，不碰生产）")
    # ------------------------------------------------------------------
    engine = _make_engine()
    c.check("引擎持有结晶档", hasattr(engine, "crystallized"))
    c.check("技能库落在沙箱内", str(engine.crystallized.store_path).startswith(str(SANDBOX)),
            f"实际 {engine.crystallized.store_path}")
    c.check("结晶档落在沙箱内的运行区路径（procedural_memory/index.jsonl）",
            engine.crystallized.store_path.name == "index.jsonl"
            and engine.crystallized.store_path.parent.name == "procedural_memory",
            str(engine.crystallized.store_path))

    # ------------------------------------------------------------------
    c.section("第 2 部分：L1 注入 —— 空库时不注入")
    # ------------------------------------------------------------------
    messages = engine._build_messages("今天天气适合打药吗")
    system = messages[0]["content"]
    c.check("system 提示已生成", messages[0]["role"] == "system" and len(system) > 100)
    c.check("空技能库 → 不出现【历史处理路径】", "【历史处理路径】" not in system)

    # ------------------------------------------------------------------
    c.section("第 3 部分：L1 注入 —— 有匹配条目时注入（含反锁死）")
    # ------------------------------------------------------------------
    engine.crystallized.record_trajectory(
        ["get_weather", "query_cotton_stats"],
        {"get_weather": ["location", "date"], "query_cotton_stats": ["region", "year"]},
        triggers=["打药", "天气", "能不能", "适合"],
        question="今天能不能打药",
    )
    messages = engine._build_messages("今天天气适合打药吗")
    system = messages[0]["content"]
    c.check("匹配命中 → 出现【历史处理路径】", "【历史处理路径】" in system)
    c.check("注入了工具链路径", "get_weather>query_cotton_stats" in system)
    c.check("注入了反锁死语句", "以当前问题为准" in system)
    c.check("声明了「不是答案」", "不是答案" in system)
    # ⚠️ 结晶档与手写技能**共用同一个 prompt 槽**，顺序是「结晶 → 技能 → 背景知识」。
    # 所以量结晶段时必须切到**下一个段头**，否则量到的是两段之和（本轮实测踩到）。
    _next = "【技能 · 编排指导】" if "【技能 · 编排指导】" in system else "【背景知识】"
    _crys_span = system.split("【历史处理路径】")[1].split(_next)[0]
    c.check("结晶段不超硬上限（300 字）", len(_crys_span) <= 320,
            f"实际 {len(_crys_span)}")
    c.check("不匹配的问句不注入",
            "【历史处理路径】" not in engine._build_messages("2026年棉花补贴政策")[0]["content"])

    # ------------------------------------------------------------------
    c.section("第 4 部分：只记参数名，绝不记参数值（决策 D1）")
    # ------------------------------------------------------------------
    calls = _install_fake_llm(engine)
    list(engine.ask("今天天气适合打药吗"))
    c.check("假 LLM 被调用（工具轮 + 流式各一次）",
            calls["chat"] == 2 and calls["stream"] == 1, f"实际 {calls}")

    entries = engine.crystallized.all_entries()
    blob = json.dumps(entries, ensure_ascii=False)
    c.check("ask() 之后技能库有登记条目", len(entries) >= 1, f"实际 {len(entries)}")
    c.check("条目含两个工具的签名",
            any(e["signature"] == "get_weather>query_cotton_stats" for e in entries))
    c.check("**不含参数值**（无「阿拉尔」「阿克苏」「2026-10-15」）",
            "阿拉尔" not in blob and "阿克苏" not in blob and "2026-10-15" not in blob)
    c.check("只含参数名", any(
        sorted(e["arg_shape"].get("get_weather", [])) == ["date", "location"]
        for e in entries
    ), f"实际 {[e.get('arg_shape') for e in entries]}")

    # ------------------------------------------------------------------
    c.section("第 5 部分：去重在真实链路上生效（第二轮不涨条数）")
    # ------------------------------------------------------------------
    before = engine.crystallized.counts()["entries"]
    list(engine.ask("今天天气适合打药吗"))
    after = engine.crystallized.counts()["entries"]
    c.check("第二轮同轨迹不新增条目（G4 去重）", after == before,
            f"{before} → {after}")
    hit = [e for e in engine.crystallized.all_entries()
           if e["signature"] == "get_weather>query_cotton_stats"]
    c.check("命中的条目 hit_count 已累加", hit and hit[0]["hit_count"] >= 1,
            f"实际 {[e.get('hit_count') for e in hit]}")

    # ------------------------------------------------------------------
    c.section("第 6 部分：G2 / G3 在接线上真的拦得住")
    # ------------------------------------------------------------------
    st = engine.crystallized
    good_trace = [{"name": "plot_trend", "params": ["region"], "ok": True, "round": 0}]
    bad_trace = [{"name": "plot_trend", "params": ["region"], "ok": False, "round": 0}]

    r = st.record_trajectory(["t_probe"])          # 占位，确保库非空逻辑走通
    n0 = st.counts()["entries"]

    engine._record_skill_trajectory(bad_trace, question="画个趋势", answered=True)
    c.check("G2 工具失败 → 不入库", st.counts()["entries"] == n0,
            f"{n0} → {st.counts()['entries']}")

    engine._record_skill_trajectory(good_trace, question="画个趋势", answered=False)
    c.check("G3 未产出回答 → 不入库", st.counts()["entries"] == n0,
            f"{n0} → {st.counts()['entries']}")

    engine._record_skill_trajectory(good_trace, question="画个趋势", answered=True)
    c.check("G2/G3 都过 → 正常入库", st.counts()["entries"] == n0 + 1,
            f"{n0} → {st.counts()['entries']}")

    engine._record_skill_trajectory([], question="空轨迹", answered=True)
    c.check("空轨迹不登记（G1）", st.counts()["entries"] == n0 + 1)

    # ------------------------------------------------------------------
    c.section("第 7 部分：_tool_result_ok 判定（G2 的判据）")
    # ------------------------------------------------------------------
    c.check("正常 JSON → 成功", RAGEngine._tool_result_ok(json.dumps({"data": [1]})))
    c.check("带非空 error → 失败",
            RAGEngine._tool_result_ok(json.dumps({"error": "查不到该地区"})) is False)
    c.check("error 为空串 → 成功",
            RAGEngine._tool_result_ok(json.dumps({"error": ""})) is True)
    c.check("非 JSON 文本 → 保守视为成功（宁可少记，不误拦）",
            RAGEngine._tool_result_ok("plain text") is True)
    c.check("空串 → 成功", RAGEngine._tool_result_ok("") is True)

    # ------------------------------------------------------------------
    c.section("第 8 部分：落盘真实、且与画像库互不干扰")
    # ------------------------------------------------------------------
    c.check("技能库文件已落盘", SKILL_FILE.exists())
    lines = [ln for ln in SKILL_FILE.read_text("utf-8").splitlines() if ln.strip()]
    c.check("落盘为 JSONL 且行数等于条目数",
            len(lines) == engine.crystallized.counts()["entries"],
            f"{len(lines)} vs {engine.crystallized.counts()['entries']}")
    c.check("每行可独立解析", all(isinstance(json.loads(ln), dict) for ln in lines))
    c.check("接线未向画像库写入任何条目（技能库不碰 L4 实体记忆）",
            engine.memory.counts() == {"user_memory": 0, "session_summaries": 0},
            f"实际 {engine.memory.counts()}")

    # ------------------------------------------------------------------
    c.section("第 10 部分：手写技能加载与注入（只读挂载点，零 LLM）")
    # ------------------------------------------------------------------
    lib = engine.skill_lib
    c.check("加载器扫到了技能", len(lib.all_skills()) >= 1,
            f"{[s.id for s in lib.all_skills()]}")
    c.check("无解析失败记录（errors 为空）", lib.errors() == [], str(lib.errors()))

    hits = lib.match("今天阿拉尔适合打药吗")
    c.check("触发词命中打药技能",
            [s.id for s in hits][:1] == ["spray_timing"], str([s.id for s in hits]))
    c.check("不相关的问句不命中", lib.match("2026年棉花补贴政策") == [])

    sk = hits[0]
    c.check("解析出 tools_used 且指向现有工具",
            sk.tools_used == ("get_weather",), str(sk.tools_used))
    c.check("解析出验收用例", len(sk.acceptance) == 2, str(len(sk.acceptance)))
    c.check("正文已去掉 H1（避免与技能名重复）",
            not sk.body.lstrip().startswith("# "))

    sec = lib.format_section(hits)
    c.check("渲染段落含技能名", "打药适宜性判断" in sec)
    c.check("渲染段落含编排步骤", "编排步骤" in sec)
    c.check("渲染段落含反锁死声明", "以当前问题为准" in sec)
    c.check("段落长度在上限内", len(sec) <= MAX_SKILL_SECTION_CHARS, str(len(sec)))

    sysmsg = engine._build_messages("今天阿拉尔适合打药吗")[0]["content"]
    c.check("★ 技能段真的进了 system prompt", "【技能 · 编排指导】" in sysmsg)
    c.check("system prompt 含编排步骤", "编排步骤" in sysmsg)
    c.check("system prompt 仍含背景知识段（未被挤掉）", "【背景知识】" in sysmsg)

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"技能库接线守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())