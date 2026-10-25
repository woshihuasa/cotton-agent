# -*- coding: utf-8 -*-
"""移交与续跑守卫（HANDOFF 第 1–7 步）。

【它守什么】`docs/HANDOFF.md` 的设计：主链路识别出「依赖链走不完」后，
把任务移交给编排层（可续跑、可签字、可静态校验）。

【为什么必须先有守卫】这个项目反复强调的纪律：
**"写了没跑过的守卫等于不存在"**。而更早的教训是 U1 的门禁第一版形同虚设 ——
它把 ε=0 算进"可用区"，于是永远通过。所以本文件的重点是：
**证明这条路真的会触发、也真的会失败。**

【用法】python tests/check_handoff.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ★★ 数据隔离（必须在 import core.* 之前设置）——
#   开发模式下数据目录固定为项目根，不隔离的话跑守卫会往**真实数据**里写：
#   会话（session_state.json）、L4 记忆（chroma_user_memory/）全都污染。
#   实测过一次：几轮下来多出 85 个空会话。
#
# ★ 用**固定路径**而不是 mkdtemp：
#   ① mkdtemp 每次新建一个目录，跑 N 次就留 N 个（实测累积到 12 个）；
#   ② atexit 清理在 Windows 上不可靠 —— chromadb 的 sqlite 句柄还没释放，
#      rmtree 失败而 ignore_errors 把它吞掉，看起来清理了其实没有。
#   改成固定路径 + **启动时清空**，残留最多一个，且每次运行都是干净状态。
import os as _os
import shutil as _shutil
from pathlib import Path as _Path

_TEST_DIR = _Path(__file__).resolve().parent / "eval_outputs" / "_tmp_data"
#   ↑ 落在 tests/eval_outputs/ 下，已被 .gitignore 覆盖
_shutil.rmtree(_TEST_DIR, ignore_errors=True)
_TEST_DIR.mkdir(parents=True, exist_ok=True)
_os.environ.setdefault("COTTON_AGENT_DATA_DIR", str(_TEST_DIR))

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}"
              + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 步：★ LoopGuard 记录「轮次用尽但仍想调」")
    # ------------------------------------------------------------------
    from core.tools.dispatch import LoopGuard

    g = LoopGuard(max_repeat=1)
    c.check("初始 wants_more 为 False（没耗尽）", g.wants_more is False)

    g.remember("get_weather", {"city": "阿克苏"}, "晴 25C")
    g.remember("query_cotton_stats", {"region": "阿克苏"}, "967362")
    done = g.completed_calls()
    c.check("completed_calls 按顺序记录已执行调用", len(done) == 2, str(len(done)))
    c.check("记录里带 name / args / result",
            set(done[0]) >= {"name", "args", "result"}, str(sorted(done[0])))

    g.mark_rounds_exhausted(3)
    c.check("★★ 耗尽后 wants_more 变 True", g.wants_more is True)
    c.check("★ 记录用了几轮", g.rounds_used == 3, str(g.rounds_used))
    c.check("★★ completed_calls 不因耗尽而丢失（移交要带走）",
            len(g.completed_calls()) == 2, str(len(g.completed_calls())))

    # 返回的是**副本** —— 外部改动不该影响内部状态
    snapshot = g.completed_calls()
    snapshot[0]["name"] = "被篡改"
    c.check("★ completed_calls 返回副本（外部改动不污染内部）",
            g.completed_calls()[0]["name"] == "get_weather")

    # ------------------------------------------------------------------
    c.section("第 2 步：★★ 区分「答完了」与「没轮次了」")
    # ------------------------------------------------------------------
    # 用假模型驱动真实的工具循环，两种结束路径各走一次。
    from core.rag_engine import RAGEngine

    class _Msg:
        """假 LLM 响应。"""

        def __init__(self, content: str = "", n_calls: int = 0) -> None:
            self.content = content
            self.tool_calls = []
            for i in range(n_calls):
                tc = type("TC", (), {})()
                tc.id = f"call_{i}"
                tc.function = type("F", (), {})()
                tc.function.name = "get_weather"
                tc.function.arguments = json.dumps({"city": "阿克苏"})
                self.tool_calls.append(tc)

    class _FakeLLM:
        """按脚本依次返回响应；脚本用完后一直返回"无工具调用"。"""

        def __init__(self, script) -> None:
            self._script = list(script)
            self._i = 0
            self._model = "fake"

        def chat_with_tools(self, messages, tools, model_name=None):
            if self._i < len(self._script):
                r = self._script[self._i]
                self._i += 1
                return r
            return _Msg("我会基于已有信息作答。")

    eng = RAGEngine.__new__(RAGEngine)          # 不触发真实初始化
    eng.llm = None
    eng._owns_user_data = True

    def _run(script):
        """跑一次工具循环，返回 (messages, wants_more, rounds)。"""
        fake = _FakeLLM(script)
        # 桩掉工具执行：只关心循环控制流，不关心工具本身
        eng._execute_tool = lambda name, args: "桩结果"
        eng._tool_result_ok = lambda r: True
        msgs = [{"role": "user", "content": "测试"}]
        out = eng._execute_tool_loop(msgs, [], None, llm=fake)
        return out, eng._last_loop_wants_more, eng._last_loop_rounds

    # ① 模型主动结束（第 2 轮不再要工具）
    _, w1, r1 = _run([_Msg("", n_calls=1), _Msg("答完了")])
    c.check("★★ 模型主动结束 → wants_more = False", w1 is False, str(w1))
    c.check("★ 主动结束时轮数是 2", r1 == 2, str(r1))

    # ② 轮次跑满仍要工具（每轮都要）—— 这是核心新增
    #   ★ 脚本长度必须**跟上限一致**：`_FakeLLM` 脚本用完后返回"无工具调用"，
    #     给少了循环会**自然 break**（wants_more=False），测的就不是"跑满"了。
    #     所以读常量而不是写死 3 —— 见 `core/rag_engine.py` 的 `MAX_TOOL_ROUNDS`。
    from core.rag_engine import MAX_TOOL_ROUNDS as _MAX_ROUNDS
    _n_rounds = _MAX_ROUNDS
    _, w2, r2 = _run([_Msg("", n_calls=1)] * _n_rounds)
    c.check("★★★ 轮次跑满仍要工具 → wants_more = True", w2 is True, str(w2))
    c.check(f"★ 跑满时轮数 = MAX_TOOL_ROUNDS({_n_rounds})", r2 == _n_rounds, str(r2))

    # ③ 第 1 轮就不要工具（正常短问）
    _, w3, r3 = _run([_Msg("直接回答")])
    c.check("★ 第 1 轮就不要工具 → wants_more = False", w3 is False, str(w3))

    # ------------------------------------------------------------------
    c.section("第 3 步：★★ 移交判定 + 携带已完成结果（不重跑）")
    # ------------------------------------------------------------------
    from core import handoff

    need, why = handoff.should_handoff(wants_more=False)
    c.check("★ 正常结束 → 不移交", need is False, why)
    c.check("★ 且给出不移交的理由", bool(why))

    need2, why2 = handoff.should_handoff(wants_more=True)
    c.check("★★ 轮次用尽仍想调 → 移交", need2 is True, why2)
    c.check("★ 理由点明依赖链（而不是目标多）", "依赖链" in why2, why2)

    done = [
        {"name": "get_weather", "args": {"city": "阿克苏"}, "result": "晴 25C"},
        {"name": "query_cotton_stats",
         "args": {"region": "阿克苏", "year": 2021}, "result": "967362 吨"},
    ]
    inputs = handoff.build_inputs(done, question="能不能收？", rounds_used=3)
    c.check("★★ done 数量正确", inputs["done_count"] == 2,
            str(inputs["done_count"]))
    c.check("★★ 每步的 name/args/result 都在",
            inputs["done"][1]["name"] == "query_cotton_stats"
            and inputs["done"][1]["args"]["year"] == 2021
            and "967362" in inputs["done"][1]["result"])
    c.check("★ 工具链按顺序记录",
            inputs["tool_chain"] == ["get_weather", "query_cotton_stats"],
            str(inputs["tool_chain"]))
    c.check("★ 记录轮数", inputs["rounds_used"] == 3)
    c.check("★ 原始问题也带上（编排层要知道在干什么）",
            inputs["question"] == "能不能收？")

    try:
        json.dumps(inputs, ensure_ascii=False)
        c.check("★★★ inputs 可 JSON 序列化（断点落盘的前提）", True)
    except (TypeError, ValueError) as e:
        c.check("★★★ inputs 可 JSON 序列化（断点落盘的前提）", False, str(e))

    class _Weird:
        def __repr__(self):
            return "<weird>"

    inputs2 = handoff.build_inputs(
        [{"name": "x", "args": {"obj": _Weird()}, "result": "ok"}])
    try:
        json.dumps(inputs2, ensure_ascii=False)
        c.check("★★ 不可序列化的参数被降级为字符串（不崩）", True)
    except (TypeError, ValueError) as e:
        c.check("★★ 不可序列化的参数被降级为字符串（不崩）", False, str(e))

    long_result = "HEAD" + ("x" * 2000) + "TAIL-967362"
    inputs3 = handoff.build_inputs(
        [{"name": "x", "args": {}, "result": long_result}])
    kept = inputs3["done"][0]["result"]
    c.check("★ 超长结果被截断", len(kept) < len(long_result), str(len(kept)))
    c.check("★★ 截断时保留尾部（关键数字常在末尾）", "967362" in kept)
    c.check("★ 截断时也保留开头", "HEAD" in kept)

    # ------------------------------------------------------------------
    c.section("第 3.5 步：★★ 部分结果提示必须说清三件事")
    # ------------------------------------------------------------------
    note = handoff.partial_notice(inputs)
    c.check("★★ 有固定标记（前端/守卫可识别）",
            handoff.PARTIAL_MARKER in note, note[:40])
    c.check("★★ 说明这不是全部", "没有走完" in note)
    c.check("★ 说明已完成到哪一步（步数 + 工具链）",
            "2 步" in note and "get_weather" in note)
    c.check("★ 说明接下来能怎么办（给用户出路）", "拆小" in note, note[-80:])

    note_ok = handoff.partial_notice(inputs, handoff_ok=True,
                                     handoff_msg="run-abc")
    c.check("★ 移交成功时措辞不同（说的是已转入而不是没走完）",
            "已尝试转入" in note_ok and "拆小" not in note_ok)

    d_no = handoff.decide(wants_more=False)
    c.check("★ decide：不移交时 inputs 为空",
            d_no.needed is False and d_no.inputs == {})
    d_yes = handoff.decide(wants_more=True, completed=done,
                           question="q", rounds_used=3)
    c.check("★★ decide：移交时 inputs 已打包",
            d_yes.needed and d_yes.inputs["done_count"] == 2)

    # ------------------------------------------------------------------
    c.section("第 4 步：★★★ YAML 生成 → 静态校验 → **回退**")
    # ------------------------------------------------------------------
    # 【这一步的重点不是"能生成"，而是"**生成错了会被挡住**"。
    #   本项目已有先例：U1 的门禁第一版形同虚设，是守卫真跑才抓出来的。】

    yaml_text = handoff.build_resume_yaml(inputs)
    c.check("★ 能生成工作流 YAML", "id: handoff_resume" in yaml_text, yaml_text[:60])
    c.check("★ 生成的流程**不含 llm 节点**（不猜、不花钱）",
            "type: llm." not in yaml_text)
    c.check("★ 含 human.confirm（链条断了要人决定）",
            "type: human.confirm" in yaml_text)
    c.check("★ 原始问题被带进提示词（人要知道在确认什么）",
            "能不能收" in yaml_text)
    c.check("★ 已完成的步数也带进去",
            "已完成 2 步" in yaml_text, yaml_text[:200])

    wf, issues = handoff.validate_workflow_text(yaml_text)
    c.check("★★ 生成的 YAML **通过静态校验**（无问题）",
            wf is not None and not issues, str(issues[:2]))

    good = handoff.prepare(inputs)
    c.check("★★ prepare() 对合法流程返回 ok", good.ok, good.message())

    # ★★★ 核心：故意造**不合格**的 YAML，必须被挡住
    BAD_CASES = [
        ("依赖成环",
         """
id: x
nodes:
  - { id: a, type: gate.range, params: { value: 1, lo: 0, hi: 10 }, needs: [b] }
  - { id: b, type: gate.range, params: { value: 1, lo: 0, hi: 10 }, needs: [a] }
"""),
        ("节点 id 重复",
         """
id: x
nodes:
  - { id: a, type: gate.range, params: { value: 1, lo: 0, hi: 10 } }
  - { id: a, type: gate.range, params: { value: 2, lo: 0, hi: 10 } }
"""),
        ("依赖不存在的节点",
         """
id: x
nodes:
  - { id: a, type: gate.range, params: { value: 1, lo: 0, hi: 10 }, needs: [不存在] }
"""),
        ("未知节点类型",
         """
id: x
nodes:
  - { id: a, type: 瞎写的类型, params: {} }
"""),
        ("必填参数缺失",
         """
id: x
nodes:
  - { id: a, type: gate.range, params: {} }
"""),
        ("YAML 语法本身坏了",
         """
id: x
nodes:
  - { id: a, type: gate.range, params: { value: 1
"""),
    ]
    for label, bad_yaml in BAD_CASES:
        wf_b, issues_b = handoff.validate_workflow_text(bad_yaml)
        caught = (wf_b is None) or bool(issues_b)
        c.check(f"★★★ 故意造【{label}】→ 校验**必须拦下**", caught,
                "没拦住！这会让坏流程真的跑起来")

    # ★ 兜底：生成阶段就出错时，prepare 返回 ok=False 而不是抛异常
    orig = handoff.build_resume_yaml
    try:
        handoff.build_resume_yaml = lambda _i: (_ for _ in ()).throw(
            RuntimeError("模拟生成失败"))
        broken = handoff.prepare(inputs)
        c.check("★★ 生成阶段抛异常 → prepare 返回 ok=False（不炸到调用方）",
                broken.ok is False and "生成工作流失败" in broken.error,
                broken.error[:60])
    finally:
        handoff.build_resume_yaml = orig

    # ★ 回退时的措辞：必须让用户知道"没转入任务模式"，而不是静默
    back_note = handoff.partial_notice(
        inputs, handoff_ok=False, handoff_msg="生成的工作流未通过静态校验")
    c.check("★★ 回退时明确告知「本次没有转入任务模式」",
            "没有转入任务模式" in back_note, back_note[-120:])

    # ------------------------------------------------------------------
    c.section("第 5 步：★★★ 端到端 —— 静默失败必须变成显式告知")
    # ------------------------------------------------------------------
    # 【这一步是本设计真正的验收点】
    #   在此之前，循环跑满 = **静默退出**：用户拿到一段看起来完整的局部答案。
    #   现在必须做到：**用户能看出来"这是部分结果"**。

    class _E2EMsg:
        def __init__(self, content="", n_calls=0):
            self.content = content
            self.reasoning_content = None
            self.tool_calls = []
            for i in range(n_calls):
                tc = type("TC", (), {})()
                tc.id = f"c{i}"
                tc.function = type("F", (), {})()
                tc.function.name = "get_weather"
                tc.function.arguments = json.dumps({"city": "阿克苏"})
                self.tool_calls.append(tc)

    class _E2ELLM:
        """永远要工具 → 把循环跑满。"""

        def __init__(self):
            self._model = "fake"
            self.last_usage = None

        def generate_response(self, messages, model_name=None, **kw):
            return "这块地能不能收"

        def chat_with_tools(self, messages, tools, model_name=None):
            return _E2EMsg("", n_calls=1)

        def stream_response(self, messages, model_name=None,
                            enable_thinking=False, reasoning_effort=None):
            yield ("content", "（桩）结论：不适合收。")

    # 临时数据目录：本用例不碰真实 session_state.json / chroma_user_memory
    import tempfile as _tf_e2e
    _e2e_dir = _tf_e2e.mkdtemp(prefix="handoff_e2e_")
    _atexit_e2e = __import__("atexit")

    def _cleanup_e2e() -> None:
        import shutil as _sh
        _sh.rmtree(_e2e_dir, ignore_errors=True)

    _atexit_e2e.register(_cleanup_e2e)

    def _stub_io(eng) -> None:
        """切断本用例的**两条网络来源**，使它真正零网络（2026-10-20 修）。

        `RAGEngine` 里有**两个**各自持有 `OpenAIEmbeddings` 的客户端，
        桩掉 `llm` 一个都不覆盖：

        | 来源 | 触发点 | 断法 |
        |---|---|---|
        | **L4 长期记忆** | `self.memory`（`UserMemoryStore`）→ `search_l4_candidates` | 构造时 `owns_user_data=False`（项目自带 `NullMemoryStore`） |
        | **知识库检索** | `self.kb`（`KnowledgeBase`）→ query embedding | 换成固定空实现 |

        ★ 为什么要做：本守卫原先**依赖网络**。抓包实测每次 `ask()` 会产生
          2–4 次连接（`127.0.0.1:7897` 本机代理 → 硅基流动）；代理抖动时抛
          `SSLError` / `ChunkedEncodingError`，于是同一条断言时绿时红
          （实测 4 次里错 3 次）。**"偶尔红"的守卫比没有守卫更糟** ——
          人会开始忽略红色，真 bug 就混过去了。

        ★ 手法与 `check_skill_wiring.py`（检索固定空值）和
          `check_memory_split.py`（字符袋向量桩件）一致。

        ★ 覆盖面不受损：本用例验的是**移交**（部分结果标记 / `wants_more` /
          `run_id` / 已完成步数），与检索命中了什么无关。
        """
        class _NullKB:
            def __getattr__(self, name):
                # 任何检索方法一律返回空 —— 即"无命中"，
                # 这是生产链路里合法的一种状态（无相关资料时走退化路径）
                def _empty(*a, **k):
                    return []
                return _empty

        eng.kb = _NullKB()

    from core.rag_engine import RAGEngine as _RE3

    try:
        # ★ `owns_user_data=False` 会让 RAGEngine 直接用项目自带的
        #   `NullMemoryStore` —— 这是**设计好的**离线路径（Web 多访客场景同款），
        #   不是测试自己造的花招。
        e2e = _RE3(user_data_dir=_e2e_dir, owns_user_data=False)
        e2e.create_session()
        e2e.llm = _E2ELLM()
        e2e._execute_tool = lambda name, args: '{"ok": true, "text": "桩天气"}'
        _stub_io(e2e)
        # ★★ 检索固定空值 —— 让本用例**真正零网络**（2026-10-20 修）。
        #
        #   为什么必须做：`_E2ELLM` 只替了 `llm` 的三个方法，但 `ask()` 里
        #   **知识库检索**用的是另一个客户端 —— `self.kb = KnowledgeBase()`，
        #   它内部的 `OpenAIEmbeddings` 会为 query 算一次真实 embedding
        #   （实测抓到的网络连接：`127.0.0.1:7897` 本机代理 → 硅基流动）。
        #
        #   后果：本守卫**依赖网络**。代理抖动时抛
        #   `SSLError: ...openaipublic...` 或 `ChunkedEncodingError`，
        #   于是同一条断言时绿时红（实测 4 次里错 3 次）——
        #   而"偶尔红"的守卫比没有守卫更糟：人会开始忽略红色。
        #
        #   手法与 `check_skill_wiring.py` / `check_memory_split.py` 一致：
        #   把检索换成固定值。本用例要验的是**移交**（部分结果标记 / wants_more /
        #   run_id），检索结果与这些断言无关 —— 所以不损失覆盖面。

        chunks = list(e2e.ask("这块地能不能收？"))
        text = "".join(ch for k, ch in chunks if k == "content")

        c.check("★★★ 跑满轮次后用户能看到「部分结果」标记",
                handoff.PARTIAL_MARKER in text, text[-120:])
        c.check("★★ 明确说了任务没走完", "没有走完" in text)
        c.check("★★ 给出已完成的步数", "前 1 步" in text or "前 2 步" in text)
        # ★ 移交已于 2026-10-26 **封存** —— 措辞从"已尝试转入任务模式（run_id=…）"
        #   退回「如实告知 + 给出可执行建议」。断言随之更新：
        #   现在要证明的是**没有启动工作流**（无 run_id）且**把话说清楚了**。
        c.check("★★★ 移交已封存：**不再出现 run_id**（工作流没有启动）",
                "run_id=" not in text, text[-200:])
        c.check("★★ 封存后给出**可执行的建议**（把问题拆小）",
                "拆小" in text, text[-200:])
        c.check("★ 说明本次没有转入任务模式的原因（不是静默失败）",
                "没有转入任务模式" in text, text[-200:])
        c.check("★★ 引擎记录了 wants_more（可被上层消费）",
                e2e._last_loop_wants_more is True)
        c.check("★ 引擎记录了完整调用（移交要带走）",
                len(e2e._last_loop_completed) >= 1,
                str(len(e2e._last_loop_completed)))

        # ★ 对照组：模型主动结束时**不该**出现部分结果标记
        class _E2EQuiet(_E2ELLM):
            def __init__(self):
                super().__init__()
                self._n = 0

            def chat_with_tools(self, messages, tools, model_name=None):
                self._n += 1
                # 第 1 轮调一个工具，第 2 轮直接结束
                return _E2EMsg("", n_calls=1) if self._n == 1 else _E2EMsg("答完了")

        try:
            quiet = _RE3(user_data_dir=_e2e_dir, owns_user_data=False)
            quiet.create_session()
            quiet.llm = _E2EQuiet()
            quiet._execute_tool = lambda name, args: '{"ok": true, "text": "桩"}'
            _stub_io(quiet)          # 同上：两条网络来源都切断
            qtext = "".join(ch for k, ch in quiet.ask("这块地能不能收？")
                            if k == "content")
            c.check("★★★ 对照组：正常结束时**不出现**部分结果标记",
                    handoff.PARTIAL_MARKER not in qtext, qtext[-100:])
        except Exception as e:                               # noqa: BLE001
            c.check("★ 对照组跑通", False, f"{type(e).__name__}: {e}")

    except Exception as e:                                   # noqa: BLE001
        c.check("★★★ 端到端跑通（不应抛异常）", False,
                f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    c.section("第 6 步：★★★ 断点续跑 —— 续跑**不重跑**已完成节点")
    # ------------------------------------------------------------------
    # 【为什么这条必须用"计数"验证】
    #   "能续跑"很容易假装：重新跑一遍也能得到 DONE。
    #   真正的判据是 **已完成节点没有被再执行一次** ——
    #   否则那几轮的 LLM 调用和工具执行就白花了（移交的意义就没了）。

    import tempfile as _tf

    from core.orchestration import engine as _orch_engine
    from core.orchestration import loader as _orch_loader
    from core.orchestration import store as _orch_store

    _CALLS = {"n": 0}
    _orig_exec = _orch_engine._EXECUTORS["tool"]

    def _counting_exec(node, ctx):
        """计数 + **固定桩返回**（不调真工具）。

        ★★ 2026-10-20 修：原先是 `_CALLS["n"] += 1; return _orig_exec(node, ctx)` ——
          只计数，但**仍然真执行** `tool.get_weather`。抓包实测每次 E2E 会产生
          2 次真实网络连接（`requests.get` → 高德天气 API，经本机代理
          `127.0.0.1:7897`）。代理抖动时守卫会抛 `SSLError` /
          `ChunkedEncodingError`，于是**同一条断言时绿时红**。

        ★ 本步要验的是**"已完成节点不被重复执行"**（靠 `_CALLS` 计数），
          以及续跑状态机 —— 与"天气数据是什么"无关。
          所以桩掉返回值即可，覆盖面不受损。

        ★ 产出结构照抄 `engine._exec_tool`：JSON 文本 + 展开到顶层的字段，
          这样 YAML 里写 `{{w1.xxx}}` 的下游照样能取到值。
        """
        _CALLS["n"] += 1
        from core.orchestration.engine import NodeOutcome as _NO
        
        payload = {"city": "阿拉尔", "weather": "晴", "temp": 18,
                   "text": "（桩）阿拉尔天气：晴 18℃"}
        out = {"text": payload["text"], "image_path": None, "image_paths": []}
        for k, v in payload.items():
            out.setdefault(k, v)
        return _NO(ok=True, output=out)

    _orch_engine._EXECUTORS["tool"] = _counting_exec

    _RESUME_YAML = (
        "id: guard_resume\n"
        "version: 1\n"
        "description: 守卫用 —— 工具 → 人工确认\n"
        "inputs:\n"
        "  city: { type: string, required: true }\n"
        "nodes:\n"
        "  - id: w1\n"
        "    type: tool.get_weather\n"
        "    params: { city: \"{{city}}\", date: today }\n"
        "    side_effect: none\n"
        "  - id: confirm\n"
        "    type: human.confirm\n"
        "    params: { prompt: \"请确认是否继续\", max_reject: 1 }\n"
        "    side_effect: none\n"
        "    needs: [w1]\n"
    )

    with _tf.TemporaryDirectory() as _td:
        _old_runs = _orch_store.runs_dir
        _orch_store.runs_dir = lambda: Path(_td)      # type: ignore
        try:
            _wf = _orch_loader.load_text(_RESUME_YAML)
            _eng = _orch_engine.Engine(_wf, persist=True)

            _CALLS["n"] = 0
            _run = _eng.run({"city": "阿克苏"})
            _first = _CALLS["n"]

            c.check("★★ 含 human.confirm 的流程会**停下来等**",
                    _run.state.name == "WAITING", _run.state.name)
            c.check("★ 停下来时已完成的节点是 DONE",
                    _run.steps["w1"].state.name == "DONE")
            c.check("★ 待确认节点是 WAITING",
                    _run.steps["confirm"].state.name == "WAITING")

            # 断点真的落盘了
            _files = [f.name for f in Path(_td).glob("*")]
            c.check("★★ 断点文件已落盘（重启不丢）",
                    any(f.endswith(".ckpt.json") for f in _files), str(_files))

            _cp = _orch_store.load_checkpoint(_run.run_id)
            c.check("★★ 断点里含已完成节点的**输出**（不是只记状态）",
                    _cp is not None and "w1" in (_cp.node_outputs or {}),
                    str(_cp.node_outputs if _cp else None)[:80])

            # ★★★ 核心：模拟进程重启（新 Engine，内存状态全丢）后续跑
            _eng2 = _orch_engine.Engine(_wf, persist=True)
            _CALLS["n"] = 0
            _run2 = _eng2.decide(_run.run_id, approved=True)
            _second = _CALLS["n"]

            c.check("★★★ 续跑时**没有重跑**已完成节点（工具执行 0 次）",
                    _second == 0, f"重跑了 {_second} 次（首次是 {_first} 次）")
            c.check("★★ 续跑后整体完成", _run2.state.name == "DONE",
                    _run2.state.name)
            c.check("★★ 已完成节点的输出**从断点恢复**（不是空）",
                    "天气" in str(_run2.steps["w1"].output or {}))
        finally:
            _orch_store.runs_dir = _old_runs          # type: ignore
            # ★ 不在这里还原工具执行器 —— 后面的流程用例（第 7 步的
            #   `tool.get_weather` 节点）同样会**真发网络请求**。
            #   统一到 main() 末尾还原，中途一律用桩。

    # ------------------------------------------------------------------
    c.section("第 7 步：★★ 人工签字 —— 拒绝也要能收场")
    # ------------------------------------------------------------------
    with _tf.TemporaryDirectory() as _td2:
        _old2 = _orch_store.runs_dir
        _orch_store.runs_dir = lambda: Path(_td2)     # type: ignore
        # ★ 原这里是装回真执行器 —— 但本步的 _RESUME_YAML 含 tool.get_weather
        #   节点，装回去就会真发一次天气请求（实测这是本守卫最后 1 次网络连接）。
        #   本步验的是签字/打回/熔断，与天气数据无关 → 保持桩即可。
        try:
            _wf2 = _orch_loader.load_text(_RESUME_YAML)
            _e3 = _orch_engine.Engine(_wf2, persist=True)
            _r3 = _e3.run({"city": "阿克苏"})
            c.check("★ 签字前流程挂起（不会自动放行）",
                    _r3.state.name == "WAITING", _r3.state.name)

            _r4 = _e3.decide(_r3.run_id, approved=False)
            c.check("★★ 拒绝后流程**不会**判成功",
                    _r4.state.name != "DONE", _r4.state.name)
            c.check("★★ 拒绝时后续节点**没有被执行**",
                    _r4.steps["confirm"].state.name != "DONE",
                    _r4.steps["confirm"].state.name)
            # ★ 实测口径：`max_reject=1` 时，拒绝一次即**熔断为 BLOCKED**。
            #   这是正确的 Fail-Closed —— 不能无限打回重试。
            c.check("★★★ 打回超上限 → **熔断**（BLOCKED，不是反复重试）",
                    _r4.state.name == "BLOCKED", _r4.state.name)
            c.check("★ 熔断原因写明（人工打回次数超限）",
                    "打回" in (_r4.steps["confirm"].error or ""),
                    _r4.steps["confirm"].error[:60])

            # ★ 熔断后再 decide 必须**明确报错**，不能静默成功
            try:
                _e3.decide(_r3.run_id, approved=True)
                c.check("★★ 熔断后再批准 → 应报错（不能静默成功）", False,
                        "居然没报错")
            except Exception:                            # noqa: BLE001
                c.check("★★ 熔断后再批准 → 明确报错（不能静默成功）", True)
        except Exception as e:                           # noqa: BLE001
            c.check("★ 签字流程跑通（不应抛异常）", False,
                    f"{type(e).__name__}: {e}")
        finally:
            _orch_store.runs_dir = _old2              # type: ignore

    # ------------------------------------------------------------------
    c.section("第 8 步：★★★ 移交**真执行**（不再只是'告知'）")
    # ------------------------------------------------------------------
    # 【这一步补的是什么】
    #   在此之前 `handoff` 只做「判定 + 打包 + 告知」——`prepare()` 返回 ok 就完了，
    #   `Engine.run()` **一次都没被调用过**。所以"主链路接入编排"这句话是空的。
    #   现在：生成 → 静态校验 → **后台真跑**，并产出可带走的存档。

    import tempfile as _tf8

    from core.task_runner import TaskRunner as _TR8

    with _tf8.TemporaryDirectory() as _td8:
        # ① 工作流应含**两个**节点（确认 + 存档），且依赖正确
        _y = handoff.build_resume_yaml(inputs)
        c.check("★★ 移交工作流含 human.confirm 与 report.partial 两个节点",
                "type: human.confirm" in _y and "type: report.partial" in _y)
        c.check("★★ 存档节点**依赖**确认节点（先签字再落盘）",
                "needs: [confirm]" in _y)

        # ② 静态校验必须过（否则压根不该启动）
        _wf8, _iss8 = handoff.validate_workflow_text(_y)
        c.check("★ 生成的工作流通过静态校验", _wf8 is not None and not _iss8,
                str(_iss8[:2]))

        # ③ ★★★ 真执行：executed=True 且拿到 run_id
        _evs8: list[str] = []
        _runner8 = _TR8(on_event=lambda e: _evs8.append(e.kind))
        _out8 = handoff.execute(dict(inputs, out_dir=_td8), runner=_runner8)
        c.check("★★★ execute() 真的启动了工作流（executed=True）",
                _out8.executed is True, _out8.error)
        c.check("★★ 拿到了 run_id（可被上层追踪）",
                bool(_out8.run_id) and _out8.run_id.startswith("handoff_resume"),
                _out8.run_id)
        c.check("★ 事件流已开始（started）", "started" in _evs8, str(_evs8))

        # ④ 它应停在 human.confirm（等人签字），而不是跑完
        _run8 = _runner8.wait(_out8.run_id, timeout=120)
        _st8 = getattr(getattr(_run8, "state", None), "name", str(_run8))
        c.check("★★★ 工作流停在 human.confirm（WAITING，不自动放行）",
                _st8 == "WAITING", _st8)
        if _run8 is not None:
            c.check("★ 确认节点 WAITING、存档节点仍 PENDING",
                    _run8.steps["confirm"].state.name == "WAITING"
                    and _run8.steps["archive"].state.name == "PENDING",
                    str({k: v.state.name for k, v in _run8.steps.items()}))

        # ⑤ ★★ 批准后应继续并**真的落盘一份存档**
        from core.orchestration import engine as _e8

        _eng8 = _e8.Engine(_wf8, persist=True)
        _run8b = _eng8.decide(_out8.run_id, approved=True,
                              inputs=dict(inputs, out_dir=_td8))
        c.check("★★★ 批准后整体 DONE", _run8b.state.name == "DONE",
                _run8b.state.name)

        _arch8 = _run8b.steps.get("archive")
        _aout8 = getattr(_arch8, "output", None) if _arch8 else None
        c.check("★★★ 存档节点产出了文件路径",
                isinstance(_aout8, dict) and bool(_aout8.get("path")),
                str(_aout8)[:80])
        if isinstance(_aout8, dict) and _aout8.get("path"):
            _ap8 = Path(_aout8["path"])
            c.check("★★★ 存档文件真的写到磁盘了", _ap8.is_file(), str(_ap8))
            _amd8 = _ap8.read_text(encoding="utf-8") if _ap8.is_file() else ""
            # guard 里的 `inputs` 用的 question 是「能不能收？」——
            # 用变量取值，避免把问题串硬编码在断言里（改一次要改两处）
            _q8 = str(inputs.get("question") or "")
            c.check("★★ 存档含原始问题（可追溯）",
                    bool(_q8) and _q8 in _amd8, f"要找 {_q8!r}")
            c.check("★★ 存档含已完成的步骤名",
                    "get_weather" in _amd8 and "query_cotton_stats" in _amd8)
            c.check("★★★ 存档**声明了边界**（不是完整结果）",
                    "不是完整结果" in _amd8 or "边界声明" in _amd8)
            c.check("★ 存档记录了步数与轮数",
                    "已完成步数" in _amd8 and "消耗轮数" in _amd8)

        # ⑥ ★★ 坏工作流**不该启动线程**（否则留一个"看起来在跑"的空任务）
        _bad8 = "id: x\nnodes:\n  - { id: a, type: 瞎写, params: {} }\n"
        _orig_build8 = handoff.build_resume_yaml
        try:
            handoff.build_resume_yaml = lambda _i: _bad8
            _r8 = _TR8(on_event=lambda e: None)
            _obad8 = handoff.execute(dict(inputs), runner=_r8)
            c.check("★★★ 工作流不合格时**不启动线程**（executed=False）",
                    _obad8.ok is False and _obad8.executed is False,
                    f"ok={_obad8.ok} executed={_obad8.executed}")
        finally:
            handoff.build_resume_yaml = _orig_build8

        # ⑦ ★★ 执行失败时**如实回退**，且措辞是"没有转入任务模式"而不是"已转入"
        _note8 = handoff.partial_notice(
            inputs, handoff_ok=False, handoff_msg="生成的工作流未通过静态校验")
        c.check("★★★ 失败时措辞是「没有转入任务模式」（不假装成功）",
                "没有转入任务模式" in _note8, _note8[-100:])
        _note8b = handoff.partial_notice(
            inputs, handoff_ok=True, handoff_msg="run_id=abc")
        c.check("★★ 成功时措辞是「已尝试转入」并带 run_id",
                "已尝试转入" in _note8b and "run_id=abc" in _note8b)

    # ------------------------------------------------------------------
    c.section("第 9 步：★ 修掉一处「说了没做」——不再承诺不存在的命令")
    # ------------------------------------------------------------------
    _rag = (ROOT / "core" / "rag_engine.py").read_text(encoding="utf-8")
    c.check("★★★ 提示里不再出现不存在的 `core.perception.cli`",
            "core.perception.cli" not in _rag)
    c.check("★★ 且确实不存在该文件（佐证上一句不是空话）",
            not (ROOT / "core" / "perception" / "cli.py").exists())
    # ★ 断言"指向了真实存在的入口"——而不是某个具体函数名。
    #   （`/perception` 现在是**真启动**，不再是"打印一段跑法提示"，
    #     所以早先断言 `build_record` 出现在 rag_engine 里已经过时。）
    c.check("★★ 改成了**真实可用**的入口（走 launch 而不是打印提示）",
            "core.perception import launch" in _rag
            or "from core.perception import launch" in _rag
            or "_plaunch" in _rag,
            "rag_engine 里没找到 launch 调用")
    c.check("★★ 且该入口**确实存在**（佐证上一句不是空话）",
            (ROOT / "core" / "perception" / "launch.py").is_file())
    c.check("★ 工作流文件也真实存在",
            (ROOT / "workflows" / "perception_report.yaml").is_file())

    # ------------------------------------------------------------------
    c.section("第 10 步：★★ 两份登记一致性（report.partial 也要两处都在）")
    # ------------------------------------------------------------------
    from core.orchestration import catalog as _cat8

    for _t8 in ("report.partial", "report.markdown", "perception.record"):
        c.check(f"★★ {_t8}：白名单 + 执行器表**两处一致**",
                (_t8 in _cat8.EXECUTABLE_TYPES)
                and (_e8.executor_for(_t8) is not None))

    # ★ 统一还原被替换的工具执行器（见第 6 步 `_counting_exec` 的说明）。
    #   中途不还原的原因：第 7 步会再跑一个带 `tool.get_weather` 的流程，
    #   还原了它就会真发请求 —— 实测那是本守卫最后 1 次网络连接。
    try:
        _orch_engine._EXECUTORS["tool"] = _orig_exec
    except (NameError, KeyError, AttributeError):     # noqa: BLE001
        pass

    print()
    print("=" * 78)
    print(f"移交与续跑守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
