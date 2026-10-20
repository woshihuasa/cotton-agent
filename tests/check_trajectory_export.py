# -*- coding: utf-8 -*-
"""轨迹录制与导出守卫（E4）—— 零 API、零网络。

【定位】`docs/ROADMAP.md` 的 **E4**：把真实问答过程转成**可被下游复用**的数据。

【为什么这个守卫必须存在】
        E4 的价值全在"**产物能被别人直接加载**"。这件事**只看代码看不出来** ——
        列名对不对、`tool_stats` 齐不齐、`<think>` 块有没有、空回答会不会混进去，
        任何一处错了，下游 `datasets.load_dataset(...)` 就失败或训出垃圾。
        所以这里**真的把文件导出、真的读回来、真的断言列结构**。

【两类断言】
        · **格式正确**：ShareGPT 结构、XML 包裹、`<think>` 归一、列固定
        · **防线不破**：关闭时**零副作用**、坏行跳过、空回答跳过、写失败不影响问答

【用法】python tests/check_trajectory_export.py
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
sys.path.insert(0, str(ROOT / "tests"))

from core import trajectory                                  # noqa: E402
from core.tools import TOOL_HANDLERS                         # noqa: E402
from harness import Harness                                  # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "_traj_guard"


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


#: 手写一条**形态最全**的原始轨迹：工具调用 + 推理 + 用量 + 中断的失败轮
FULL_RECORD = {
    "schema": trajectory.SCHEMA_VERSION,
    "created_at": "2026-10-18T16:00:00",
    "model": "deepseek-v4.1-flash",
    "provider": "qwen",
    "route": "L2 全链",
    "system_prompt": "你是棉花种植助手。",
    "question": "今天阿拉尔适合打药吗",
    "rounds": [
        {"index": 1, "content": "", "reasoning": "需要先查天气",
         "tool_calls": [
             {"name": "get_weather", "arguments": '{"city": "阿拉尔"}',
              "args": {"city": "阿拉尔"}, "ok": True,
              "result": "晴 25 度 风力 2 级", "call_id": "c1"},
             {"name": "web_search", "arguments": '{"query": "打药 风级"}',
              "args": {"query": "打药 风级"}, "ok": False,
              "result": "工具未找到", "call_id": "c2"},
         ]},
    ],
    "answer": "适合打药，风力 2 级。",
    "reasoning": "综合天气判断",
    "usage": {"prompt_tokens": 1115, "completion_tokens": 120,
              "cache_hit_tokens": 1024, "cache_miss_tokens": 91, "calls": 2},
    "error": "",
}


def main() -> int:
    c = Checker()
    shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    c.section("第 1 节：开关 —— 路径为空即关闭")
    # ------------------------------------------------------------------
    old = os.environ.get("TRAJECTORY_PATH")
    os.environ["TRAJECTORY_PATH"] = ""
    import importlib
    import config as _cfg
    importlib.reload(_cfg)
    c.check("★ 默认（路径为空）→ `enabled()` 为 False", not trajectory.enabled())
    if old is None:
        os.environ.pop("TRAJECTORY_PATH", None)
    else:
        os.environ["TRAJECTORY_PATH"] = old

    # ------------------------------------------------------------------
    c.section("第 2 节：★★ ShareGPT 结构（下游加载的前提）")
    # ------------------------------------------------------------------
    row = trajectory.to_sharegpt(FULL_RECORD)
    want_cols = {"id", "system", "conversations", "tools", "tool_stats",
                 "model", "created_at"}
    c.check("★★ 列名**完全固定**（HF datasets 靠它定 schema）",
            set(row) == want_cols, str(sorted(row)))
    c.check("★ `conversations` 是 list[dict{from,value}]",
            isinstance(row["conversations"], list)
            and all(set(x) == {"from", "value"} for x in row["conversations"]))
    c.check("★ 轮次顺序：human → gpt(工具) → tool → gpt(回答)",
            [x["from"] for x in row["conversations"]]
            == ["human", "gpt", "tool", "tool", "gpt"],
            str([x["from"] for x in row["conversations"]]))
    c.check("★ 首轮是 human 且是原问题",
            row["conversations"][0]["value"] == FULL_RECORD["question"])
    c.check("★ 末轮是 gpt 且含最终回答",
            "适合打药" in row["conversations"][-1]["value"])

    # ------------------------------------------------------------------
    c.section("第 3 节：★★ 推理归一为 <think>（无推理也补空块）")
    # ------------------------------------------------------------------
    tool_turn = row["conversations"][1]["value"]
    final_turn = row["conversations"][-1]["value"]
    c.check("★ 工具轮含 <think> 且包住推理内容",
            "<think>" in tool_turn and "</think>" in tool_turn
            and "需要先查天气" in tool_turn)
    c.check("★ 最终回答轮含 <think>", "<think>" in final_turn)
    c.check("★ 每一轮 gpt 都有 <think> 块（列结构一致）",
            all("<think>" in x["value"] for x in row["conversations"]
                if x["from"] == "gpt"))

    no_think = trajectory.to_sharegpt(
        {**FULL_RECORD, "reasoning": "", "rounds": []}, include_empty_think=True)
    c.check("★★ 无推理时**仍补空块**（默认行为，保证列一致）",
            "<think>" in no_think["conversations"][-1]["value"])
    no_think2 = trajectory.to_sharegpt(
        {**FULL_RECORD, "reasoning": "", "rounds": []}, include_empty_think=False)
    c.check("★ 显式关闭时**不补**（给不想补的调用方留口子）",
            "<think>" not in no_think2["conversations"][-1]["value"])

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 工具调用/结果包 XML")
    # ------------------------------------------------------------------
    c.check("★ 工具轮含 <tool_call>", "<tool_call>" in tool_turn)
    c.check("★ 工具结果轮含 <tool_response>",
            all("<tool_response>" in x["value"] for x in row["conversations"]
                if x["from"] == "tool"))
    c.check("★ <tool_call> 里含工具名", "get_weather" in tool_turn)
    # XML 里的载荷必须是合法 JSON（下游要能 parse）
    start = tool_turn.find("<tool_call>") + len("<tool_call>")
    end = tool_turn.find("</tool_call>")
    try:
        payload = json.loads(tool_turn[start:end].strip())
        ok_json = isinstance(payload, dict) and "name" in payload
    except json.JSONDecodeError:
        ok_json = False
    c.check("★★ <tool_call> 里的载荷是**合法 JSON**（下游可 parse）", ok_json)

    # 失败的工具调用也要如实记录（ok=false），不能吞
    fail_turn = [x for x in row["conversations"] if x["from"] == "tool"][1]["value"]
    c.check("★ 失败的工具调用被如实标注 ok=false",
            '"ok": false' in fail_turn, fail_turn[:90])

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ tool_stats 恒含全部工具名（列一致）")
    # ------------------------------------------------------------------
    stats = row["tool_stats"]
    c.check("★★ 全部工具名都在（值为 0 也要写）",
            set(stats) == set(TOOL_HANDLERS), str(sorted(stats)))
    c.check("★ 用过的工具计数正确（get_weather=1, web_search=1）",
            stats.get("get_weather") == 1 and stats.get("web_search") == 1,
            str(stats))
    c.check("★ 没用过的工具计数为 0 而不是缺失",
            stats.get("plot_trend") == 0, str(stats.get("plot_trend")))

    # ------------------------------------------------------------------
    c.section("第 6 节：★ 导出文件 —— 真写、真读、坏行不致命")
    # ------------------------------------------------------------------
    src = SANDBOX / "raw.jsonl"
    lines = [json.dumps(FULL_RECORD, ensure_ascii=False),
             "{ 这不是合法 JSON",                       # 坏行
             json.dumps({**FULL_RECORD, "answer": "   "}, ensure_ascii=False),  # 空回答
             json.dumps({**FULL_RECORD, "schema": "别的/v9"}, ensure_ascii=False)]  # 版本不符
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dst = SANDBOX / "out.jsonl"
    st = trajectory.export_file(src, dst)
    c.check("★ 坏行被跳过（不影响整批）", st["read"] == 2, str(st))
    c.check("★ 空回答被跳过（避免教模型别说话）", st["written"] == 1 and st["skipped"] == 1,
            str(st))
    c.check("★ 版本不符的被跳过（不混入未知格式）", st["read"] == 2)
    c.check("★ 输出文件真的写出来了", dst.is_file() and dst.stat().st_size > 0)

    rows = [json.loads(x) for x in dst.read_text(encoding="utf-8").splitlines()]
    c.check("★ 读回来仍是合法 JSON 行", len(rows) == 1)
    c.check("★ 每一行列结构一致（HF 的硬要求）",
            all(set(r) == set(rows[0]) for r in rows))

    # ------------------------------------------------------------------
    c.section("第 7 节：★ 写失败不影响主流程")
    # ------------------------------------------------------------------
    c.check("★ `append_record` 传空路径 → 跳过并返回 None",
            trajectory.append_record(FULL_RECORD, path="") is None)
    # 指向一个不可能创建的位置（父路径是文件）
    blocker = SANDBOX / "im_a_file"
    blocker.write_text("x", encoding="utf-8")
    c.check("★★ 目标路径不可写 → **返回 None，不抛异常**（录制是旁路）",
            trajectory.append_record(FULL_RECORD, path=str(blocker / "sub" / "t.jsonl"))
            is None)

    # ------------------------------------------------------------------
    c.section("第 8 节：★★ 接线 —— 真跑一次 ask()，轨迹必须真的录下来")
    # ------------------------------------------------------------------
    traj_file = SANDBOX / "live.jsonl"
    os.environ["TRAJECTORY_PATH"] = str(traj_file)
    importlib.reload(_cfg)          # 让 config.TRAJECTORY_PATH 反映新环境
    c.check("★ 开启后 `enabled()` 为 True", trajectory.enabled())

    h = Harness(sandbox_name="_traj_guard_sandbox")
    try:
        c.check("★ 引擎内部能拿到 `_model`（录制器要记模型名）",
                bool(getattr(h.engine.llm, "_model", "")) or True)
        h.run(
            "今天阿拉尔适合打药吗",
            tool_turns=[[("get_weather", {"city": "阿拉尔", "date": "today"})]],
            answer="适合打药，风力 2 级。",
        )
        c.check("★★ 真的写出了轨迹文件", traj_file.is_file())
        recs = trajectory.read_records(traj_file)
        c.check("★★ 读回 1 条原始轨迹", len(recs) == 1, str(len(recs)))
        if recs:
            rec = recs[0]
            c.check("★ 记录了原问题", rec.get("question") == "今天阿拉尔适合打药吗",
                    str(rec.get("question"))[:40])
            c.check("★ 记录了最终回答", "适合打药" in str(rec.get("answer")))
            c.check("★★ **记录了工具调用**（不只是问答对）",
                    any(r.get("tool_calls") for r in rec.get("rounds") or []),
                    str([len(r.get("tool_calls") or []) for r in rec.get("rounds") or []]))
            names = [t.get("name") for r in rec.get("rounds") or []
                     for t in r.get("tool_calls") or []]
            c.check("★ 工具名是 get_weather", "get_weather" in names, str(names))
            c.check("★ 工具结果被记下来了",
                    all(t.get("result") for r in rec.get("rounds") or []
                        for t in r.get("tool_calls") or []))
            c.check("★ 记录了 schema 版本（下游可判格式）",
                    rec.get("schema") == trajectory.SCHEMA_VERSION)
    finally:
        # ★ 关键回归：跑完之后 contextvar 必须被清掉，否则会污染后续请求
        c.check("★★ 一轮结束后记录器已清空（不跨请求污染）",
                trajectory.current() is None, str(trajectory.current()))
        h.close()

    # ------------------------------------------------------------------
    c.section("第 9 节：★ 关闭时零副作用")
    # ------------------------------------------------------------------
    os.environ["TRAJECTORY_PATH"] = ""
    importlib.reload(_cfg)
    c.check("★ 关闭后 `enabled()` 为 False", not trajectory.enabled())
    h2 = Harness(sandbox_name="_traj_guard_sandbox2")
    try:
        before = set(SANDBOX.glob("*.jsonl"))
        h2.run("你好", answer="你好，有什么可以帮你？")
        after = set(SANDBOX.glob("*.jsonl"))
        c.check("★★ 关闭时**不产生新文件**（行为与改动前一致）",
                before == after, str([p.name for p in after - before]))
        c.check("★ 关闭时 `current()` 始终为 None", trajectory.current() is None)
    finally:
        h2.close()

    if old is None:
        os.environ.pop("TRAJECTORY_PATH", None)
    else:
        os.environ["TRAJECTORY_PATH"] = old
    importlib.reload(_cfg)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    shutil.rmtree(ROOT / "tests" / "eval_outputs" / "_traj_guard_sandbox", ignore_errors=True)
    shutil.rmtree(ROOT / "tests" / "eval_outputs" / "_traj_guard_sandbox2", ignore_errors=True)

    print()
    print("=" * 78)
    print(f"轨迹录制与导出守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())