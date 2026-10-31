# -*- coding: utf-8 -*-
"""联网搜索后端守卫 —— 换后端不该改变模型看到的东西。

【为什么要单独守】`web_search` 从"只有 Tavily"变成"Tavily / DeepSeek 可切换"，
风险不在能不能搜到，而在三件更隐蔽的事：

  ① **默认行为漂移**：`auto` 若先选 DeepSeek，同一句查询会突然换一家搜索引擎、
     结果排序也变了 —— 没人会想到是这个原因。所以 `auto` 必须**优先 Tavily**。
  ② **输出形状分叉**：两个后端各自拼字符串，`标题/摘要/---` 一旦不一致，
     提示词里"看标题与摘要"的约定会时灵时不灵。
  ③ **静默换家**：指定了 tavily 却没配 Key 时若偷偷用 DeepSeek，
     "为什么结果变了"就变成无头案。缺 Key 必须报错。

【零网络】HTTP 与 Tavily 客户端全部替掉，本文件不发任何请求。
【用法】python tests/check_web_search_backend.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_search_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from config import AppConfig                                  # noqa: E402
from core.tools import external as ex                         # noqa: E402


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


def section_pick(c: Checker) -> None:
    c.section("第 1 节：选后端（auto 必须优先 Tavily，保持原有行为）")

    both = dict(tavily_key="tv", deepseek_key="ds")
    c.check("★ auto + 两个 Key 都有 → Tavily（升级不改默认行为）",
            ex.pick_backend("", **both)[0] == "tavily", str(ex.pick_backend("", **both)))
    c.check("auto + 只有 Tavily → Tavily",
            ex.pick_backend("auto", tavily_key="tv", deepseek_key="")[0] == "tavily")
    c.check("★ auto + 只有 DeepSeek → DeepSeek（没有 Tavily 才退）",
            ex.pick_backend("auto", tavily_key="", deepseek_key="ds")[0] == "deepseek")
    name, why = ex.pick_backend("auto", tavily_key="", deepseek_key="")
    c.check("★ auto + 都没有 → 报错并说明两个都没配",
            name == "" and "Tavily" in why and "DeepSeek" in why, why)

    c.check("强制 tavily 且有 Key → tavily",
            ex.pick_backend("tavily", **both)[0] == "tavily")
    name, why = ex.pick_backend("tavily", tavily_key="", deepseek_key="ds")
    c.check("★ 强制 tavily 但没 Key → 报错，**不静默换 DeepSeek**",
            name == "" and "TAVILY_API_KEY" in why, why)

    c.check("强制 deepseek 且有 Key → deepseek",
            ex.pick_backend("deepseek", **both)[0] == "deepseek")
    name, why = ex.pick_backend("deepseek", tavily_key="tv", deepseek_key="")
    c.check("★ 强制 deepseek 但没 Key → 报错，不静默换 Tavily",
            name == "" and "DEEPSEEK_API_KEY" in why, why)

    name, why = ex.pick_backend("bing", **both)
    c.check("不认识的后端名 → 报错并列出可选项",
            name == "" and "bing" in why, why)

    c.check("大小写与空白被归一",
            ex.pick_backend("  TAVILY  ", **both)[0] == "tavily")


def section_format(c: Checker) -> None:
    c.section("第 2 节：输出形状与后端无关")

    text = ex._format_results([("标题甲", "摘要甲"), ("标题乙", "")])
    c.check("有标题有摘要 → 三行一块", "标题: 标题甲" in text and "摘要: 摘要甲" in text, text)
    c.check("只有标题 → 不输出空摘要行", text.count("摘要:") == 1, text)
    c.check("每块以 --- 结尾", text.count("---") == 2, text)

    c.check("全空条目被跳过", ex._format_results([("", ""), ("", "")]) == "")
    c.check("空白字符串被 strip", ex._format_results([("  ", "  摘要  ")])
            .startswith("摘要: 摘要"))


def section_deepseek(c: Checker) -> None:
    c.section("第 3 节：DeepSeek 后端（解析、错误、空结果）")

    class _Resp:
        def __init__(self, payload) -> None:
            self._p = payload
        def raise_for_status(self) -> None:
            return None
        def json(self):
            return self._p

    real_post = ex.requests.post
    real_key = AppConfig.DEEPSEEK_API_KEY
    AppConfig.DEEPSEEK_API_KEY = "ds-test-key"

    payload = {"results": [
        {"title": "棉花期货站上 15000", "summary": "长摘要内容甲", "snippet": "短摘要甲"},
        {"title": "新疆棉价持稳", "summary": "长摘要内容乙", "snippet": "短摘要乙"},
    ]}
    sent = {}

    def _post(url, headers=None, json=None, timeout=None):    # noqa: A002
        sent.update({"url": url, "headers": headers, "body": json})
        return _Resp(payload)

    ex.requests.post = _post
    try:
        text = ex._search_deepseek("今天棉花价格")
    finally:
        ex.requests.post = real_post
        AppConfig.DEEPSEEK_API_KEY = real_key

    c.check("★ 用的是 summary（长摘要）而不是 snippet（短摘要）",
            "长摘要内容甲" in text and "短摘要甲" not in text, text)
    c.check("输出仍是 标题 / 摘要 / --- 的形状",
            "标题: 棉花期货站上 15000" in text and text.count("---") == 2, text)
    c.check("请求打到配置的 Search 地址",
            sent.get("url") == AppConfig.DEEPSEEK_SEARCH_URL, str(sent.get("url")))
    c.check("带 Bearer 鉴权",
            (sent.get("headers") or {}).get("Authorization", "").startswith("Bearer "),
            str(sent.get("headers")))
    c.check("★ count=3，与 Tavily 的 max_results=3 对齐",
            (sent.get("body") or {}).get("count") == 3, str(sent.get("body")))
    c.check("need_content=False（省掉正文抓取）",
            (sent.get("body") or {}).get("need_content") is False, str(sent.get("body")))

    # 无结果
    ex.requests.post = lambda *a, **k: _Resp({"results": []})
    try:
        text = ex._search_deepseek("查不到的东西")
    finally:
        ex.requests.post = real_post
    c.check("空结果 → 明确说没结果，不返回空串",
            "未返回有效结果" in text, text)

    # 官方错误格式
    ex.requests.post = lambda *a, **k: _Resp(
        {"error": {"message": "'count' must be between 1 and 50",
                   "type": "invalid_request_error", "param": None,
                   "code": "invalid_request_error"}})
    try:
        text = ex._search_deepseek("x")
    finally:
        ex.requests.post = real_post
    c.check("★ 官方错误格式 → 把 message 原样说出来",
            "between 1 and 50" in text, text)

    # 网络异常
    def _boom(*_a, **_k):
        raise ex.requests.RequestException("连接被重置")
    ex.requests.post = _boom
    try:
        text = ex._search_deepseek("x")
    finally:
        ex.requests.post = real_post
    c.check("★ 网络异常 → 返回可读消息而不是抛异常",
            "搜索失败" in text and "网络" in text, text)


def section_dispatch(c: Checker) -> None:
    c.section("第 4 节：web_search 的分派（两个后端各走各的）")

    real = (AppConfig.WEB_SEARCH_BACKEND, AppConfig.TAVILY_API_KEY,
            AppConfig.DEEPSEEK_API_KEY, ex._search_tavily, ex._search_deepseek)
    calls: list[str] = []

    def _fake_tavily(q):                                       # noqa: ANN001
        calls.append("tavily")
        return "TAVILY_RESULT"

    def _fake_ds(q):                                           # noqa: ANN001
        calls.append("deepseek")
        return "DEEPSEEK_RESULT"

    ex._search_tavily, ex._search_deepseek = _fake_tavily, _fake_ds
    try:
        AppConfig.TAVILY_API_KEY, AppConfig.DEEPSEEK_API_KEY = "tv", "ds"

        AppConfig.WEB_SEARCH_BACKEND = "auto"
        c.check("auto + 两个 Key → 走 Tavily",
                ex.web_search({"query": "q"}) == "TAVILY_RESULT", str(calls))

        AppConfig.WEB_SEARCH_BACKEND = "deepseek"
        calls.clear()
        c.check("指定 deepseek → 走 DeepSeek",
                ex.web_search({"query": "q"}) == "DEEPSEEK_RESULT", str(calls))

        AppConfig.WEB_SEARCH_BACKEND = "tavily"
        AppConfig.TAVILY_API_KEY = ""
        calls.clear()
        out = ex.web_search({"query": "q"})
        c.check("★ 指定 tavily 但没 Key → 报错，且**没有调用任何后端**",
                "错误" in out and not calls, f"{out} / {calls}")

        AppConfig.WEB_SEARCH_BACKEND = "auto"
        AppConfig.TAVILY_API_KEY = ""
        AppConfig.DEEPSEEK_API_KEY = ""
        calls.clear()
        out = ex.web_search({"query": "q"})
        c.check("★ 两个都没配 → 报错，不调用后端", "错误" in out and not calls, out)

        AppConfig.DEEPSEEK_API_KEY = "ds"
        c.check("auto + 只有 DeepSeek Key → 走 DeepSeek",
                ex.web_search({"query": "q"}) == "DEEPSEEK_RESULT")

        c.check("空 query → 直接报错，不调用后端",
                "错误" in ex.web_search({"query": "   "}), "")
    finally:
        (AppConfig.WEB_SEARCH_BACKEND, AppConfig.TAVILY_API_KEY,
         AppConfig.DEEPSEEK_API_KEY, ex._search_tavily, ex._search_deepseek) = real


def section_tavily_unchanged(c: Checker) -> None:
    c.section("第 5 节：Tavily 路径行为不变（输出形状与原来一致）")

    class _Client:
        def __init__(self, api_key=None) -> None:
            self.api_key = api_key
        def search(self, **_k):
            return {"results": [{"title": "甲", "content": "内容甲"},
                                {"title": "", "content": "只有内容"}]}

    fake_mod = types.ModuleType("tavily")
    fake_mod.TavilyClient = _Client
    sys.modules["tavily"] = fake_mod
    real_key = AppConfig.TAVILY_API_KEY
    AppConfig.TAVILY_API_KEY = "tv-test"
    try:
        text = ex._search_tavily("q")
    finally:
        AppConfig.TAVILY_API_KEY = real_key
        sys.modules.pop("tavily", None)

    c.check("Tavily 的 content 仍映射到「摘要」", "摘要: 内容甲" in text, text)
    c.check("没有标题的条目也保留（只有摘要）",
            "摘要: 只有内容" in text, text)
    c.check("仍是 标题/摘要/--- 的形状", text.count("---") == 2, text)


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_pick(c)
        section_format(c)
        section_deepseek(c)
        section_dispatch(c)
        section_tavily_unchanged(c)
    finally:
        print()
        print("=" * 78)
        print(f"搜索后端守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())