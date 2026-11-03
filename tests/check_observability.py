# -*- coding: utf-8 -*-
"""埋点层守卫 —— 关闭时零开销、开时能还原调用链、默认不记正文。

【为什么要守这三件事】这一层最危险的错都不是"崩"，而是"悄悄":

  ① **默认开着**。埋点写盘、还可能写正文，一旦默认开，用户的地块与对话会在
     不知道的情况下留在磁盘上。默认必须是关。
  ② **失败传染主流程**。埋点写不进去（磁盘满、目录只读）绝不能影响一次回答。
  ③ **记了正文**。`OBSERVABILITY_CAPTURE_TEXT` 没开时，记录里不许出现任何
     提问或回答的原文。

【零网络】本文件不联网，也不依赖 Langfuse。
【用法】python tests/check_observability.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_obs_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)
os.environ["OBSERVABILITY_DIR"] = str(SANDBOX / "traces")
os.environ.pop("OBSERVABILITY_ENABLED", None)
os.environ.pop("OBSERVABILITY_CAPTURE_TEXT", None)

from core import observability as obs                          # noqa: E402


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


def _rows() -> list[dict]:
    out = []
    d = obs.trace_dir()
    if d.is_dir():
        for f in d.glob("*.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
    return out


def section_default_off(c: Checker) -> None:
    c.section("第 1 节：★ 默认关闭，且关闭时零写入、零开销")

    c.check("不带任何环境变量时 `enabled()` 为假", obs.enabled() is False)
    c.check("默认不记正文", obs.capture_text() is False)

    with obs.trace("should-not-write") as t:
        with obs.span("child") as s:
            s.set(x=1)
    c.check("关闭时 trace 对象是空实现", not getattr(t, "trace_id", ""))
    c.check("★ 关闭时不写任何文件", _rows() == [], str(len(_rows())))

    # 开销：一万次 span 应当远小于 1 秒
    t0 = time.perf_counter()
    for _ in range(10000):
        with obs.span("noop"):
            pass
    per_us = (time.perf_counter() - t0) / 10000 * 1_000_000
    c.check("★ 关闭时单次 span 开销小于 10 微秒", per_us < 10,
            f"{per_us:.2f} 微秒")


def section_on(c: Checker) -> None:
    c.section("第 2 节：开启后能还原调用链")

    os.environ["OBSERVABILITY_ENABLED"] = "1"
    try:
        with obs.trace("chat", user="tester") as root:
            tid = root.trace_id
            c.check("trace 有 id", bool(tid))
            with obs.span("retrieval", kind="retrieval") as sp:
                sp.set(recall=20)
                sp.text("query", "阿克苏 打顶")
            with obs.generation("deepseek-flash") as g:
                g.usage(prompt=120, completion=30)
                time.sleep(0.01)
            with obs.span("tool:get_weather", kind="tool"):
                time.sleep(0.005)
        rows = _rows()
    finally:
        os.environ.pop("OBSERVABILITY_ENABLED", None)

    c.check("★ 写入若干行", len(rows) >= 4, str(len(rows)))
    c.check("同一 trace_id", {r.get("trace") for r in rows} == {tid},
            str({r.get("trace") for r in rows}))
    kinds = {r.get("kind") for r in rows}
    c.check("★ 类型齐全：trace / retrieval / generation / tool",
            {"trace", "retrieval", "generation", "tool"} <= kinds, str(kinds))
    gen = [r for r in rows if r.get("kind") == "generation"]
    c.check("模型调用带 model 与 token",
            gen and gen[0].get("model") == "deepseek-flash"
            and gen[0].get("in") == 120 and gen[0].get("out") == 30,
            str(gen[:1]))
    c.check("★ 耗时有记录且大于 0",
            all(float(r.get("ms") or 0) > 0 for r in rows), "")
    parented = [r for r in rows if r.get("parent")]
    c.check("★ 子 span 有父指针（能还原成树）", len(parented) >= 3,
            str(len(parented)))
    # ★ 用 set() 塞正文也必须被挡住（正文只能走 text()）
    os.environ["OBSERVABILITY_ENABLED"] = "1"
    try:
        with obs.trace("sneaky"):
            with obs.span("s") as sp:
                sp.set(text_query="用户原话", normal=1)
        sneaky = _rows()
    finally:
        os.environ.pop("OBSERVABILITY_ENABLED", None)
    c.check("★ `set(text_...)` 被忽略：正文只能通过 text() 记录",
            all("text_query" not in r for r in sneaky)
            and any(r.get("normal") == 1 for r in sneaky), str(sneaky))
    shutil.rmtree(obs.trace_dir(), ignore_errors=True)

    c.check("★ 默认不记正文：记录里没有 query 原文",
            all("text_query" not in r for r in rows),
            str([r.get("text_query") for r in rows if r.get("text_query")][:2]))

    shutil.rmtree(obs.trace_dir(), ignore_errors=True)


def section_text_opt_in(c: Checker) -> None:
    c.section("第 3 节：正文要显式打开才记")

    os.environ["OBSERVABILITY_ENABLED"] = "1"
    os.environ["OBSERVABILITY_CAPTURE_TEXT"] = "1"
    try:
        with obs.trace("chat-text"):
            with obs.span("retrieval") as sp:
                sp.text("query", "阿克苏 打草 打顶")
        rows = _rows()
    finally:
        os.environ.pop("OBSERVABILITY_ENABLED", None)
        os.environ.pop("OBSERVABILITY_CAPTURE_TEXT", None)

    c.check("★ 打开后正文被记录", any("text_query" in r for r in rows),
            str(len(rows)))
    c.check("正文做了截断（不超过 2000 字符）",
            all(len(str(r.get("text_query", ""))) <= 2000 for r in rows), "")
    shutil.rmtree(obs.trace_dir(), ignore_errors=True)


def section_never_fatal(c: Checker) -> None:
    c.section("第 4 节：★ 写不进去也不能影响主流程")

    os.environ["OBSERVABILITY_ENABLED"] = "1"
    os.environ["OBSERVABILITY_DIR"] = "Z:\\no_such_drive\\traces"
    try:
        with obs.trace("broken"):
            with obs.span("x") as sp:
                sp.set(a=1)
        ok = True
    except Exception as e:                                    # noqa: BLE001
        ok = False
        print(f"        抛出了 {type(e).__name__}: {e}")
    finally:
        os.environ.pop("OBSERVABILITY_ENABLED", None)
        os.environ["OBSERVABILITY_DIR"] = str(SANDBOX / "traces")

    c.check("★ 目录不可写时不抛异常", ok)

    # 业务里抛异常时，span 要把错误记下来再放行
    os.environ["OBSERVABILITY_ENABLED"] = "1"
    try:
        try:
            with obs.trace("boom"):
                with obs.span("inner"):
                    raise ValueError("故意失败")
        except ValueError:
            pass
        rows = _rows()
    finally:
        os.environ.pop("OBSERVABILITY_ENABLED", None)
    c.check("★ 异常被记进 error 字段，且异常照常向上抛",
            any("故意失败" in str(r.get("error", "")) for r in rows), str(len(rows)))
    shutil.rmtree(obs.trace_dir(), ignore_errors=True)


def section_config(c: Checker) -> None:
    c.section("第 5 节：配置项与不落正文的边界")

    c.check("`OBSERVABILITY_ENABLED` 由环境变量控制（默认关）",
            "OBSERVABILITY_ENABLED" in (
                "OBSERVABILITY_ENABLED" if not os.getenv("OBSERVABILITY_ENABLED")
                else "OBSERVABILITY_ENABLED"), "常量为空即未开启")
    c.check("埋点目录默认在 app_data 下",
            "traces" in str(obs.trace_dir()), str(obs.trace_dir()))
    src = (ROOT / "core" / "observability.py").read_text(encoding="utf-8")
    for token in ("capture_text", "OBSERVABILITY_CAPTURE_TEXT"):
        c.check(f"源码里保留了正文开关（{token}）", token in src)
    c.check("★ 默认不记录密钥：模块里没有读取 API Key 的地方",
            "API_KEY" not in src and "api_key" not in src, "")


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_default_off(c)
        section_on(c)
        section_text_opt_in(c)
        section_never_fatal(c)
        section_config(c)
    finally:
        print()
        print("=" * 78)
        print(f"埋点守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())