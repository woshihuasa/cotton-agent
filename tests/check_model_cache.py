# -*- coding: utf-8 -*-
"""模型清单缓存守卫 —— 「不用每次先点测试连接」靠它。

【为什么要守】这个小模块的行为直接影响两件事：
  ① **界面打开是不是还得联网**：构造对话框只读缓存，缓存坏了/读不出也必须安静降级；
  ② **两边的清单会不会串**：LLM 与 Embedding 可能有同名服务商（都叫 openai），
     命名空间不分开就会互相覆盖 —— 表现是"选模型时看到一堆 embedding 模型"。

【零网络】纯文件读写。
【用法】python tests/check_model_cache.py
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

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_cache_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from core import model_cache as mc                             # noqa: E402


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


def section_roundtrip(c: Checker) -> None:
    c.section("第 1 节：存取往返")

    c.check("没有缓存时返回空列表（不是 None）", mc.get("llm", "nope") == [])
    mc.put("llm", "qwen", ["qwen3.7-plus", "qwen-max"])
    c.check("★ 存进去能取出来",
            mc.get("llm", "qwen") == ["qwen3.7-plus", "qwen-max"], str(mc.get("llm", "qwen")))
    c.check("取到的时间戳是最近一次", (time.time() - mc.fetched_at("llm", "qwen")) < 30)
    c.check("从未拉过的时间戳为 0", mc.fetched_at("llm", "never") == 0.0)

    mc.put("llm", "qwen", ["qwen3.8-max"])
    c.check("重复 put 覆盖旧清单", mc.get("llm", "qwen") == ["qwen3.8-max"])

    mc.put("llm", "qwen", [])
    c.check("★ 空清单**不覆盖**已有数据（拉失败时别把好的冲掉）",
            mc.get("llm", "qwen") == ["qwen3.8-max"], str(mc.get("llm", "qwen")))

    c.check("forget 删掉一条", mc.forget("llm", "qwen") is True)
    c.check("删掉之后取不到", mc.get("llm", "qwen") == [])
    c.check("forget 不存在的返回 False", mc.forget("llm", "qwen") is False)


def section_namespace(c: Checker) -> None:
    c.section("第 2 节：★ 两个命名空间互不串")

    mc.put("llm", "openai", ["gpt-a"])
    mc.put("embedding", "openai", ["text-embedding-b"])
    c.check("★ 同名服务商在两边的清单**不同**",
            mc.get("llm", "openai") == ["gpt-a"]
            and mc.get("embedding", "openai") == ["text-embedding-b"],
            f"{mc.get('llm','openai')} / {mc.get('embedding','openai')}")

    mc.forget("embedding", "openai")
    c.check("★ 删一边不影响另一边", mc.get("llm", "openai") == ["gpt-a"])


def section_stale(c: Checker) -> None:
    c.section("第 3 节：该不该刷新")

    c.check("没缓存 → stale（要拉一次）", mc.is_stale("llm", "fresh", now=1000.0) is True)
    mc.put("llm", "fresh", ["m1"])
    now = time.time()
    c.check("刚拉过 → 不 stale", mc.is_stale("llm", "fresh", now=now) is False)
    c.check("★ 超过 TTL → stale",
            mc.is_stale("llm", "fresh", ttl_s=10, now=now + 11) is True)
    c.check("★ 过期也**不清空**（旧清单比空列表有用）",
            mc.get("llm", "fresh") == ["m1"])


def section_robust(c: Checker) -> None:
    c.section("第 4 节：坏文件不炸")

    p = mc.cache_path()
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    c.check("★ 缓存文件坏了 → 返回空，不抛异常", mc.get("llm", "x") == [])
    mc.put("llm", "after_bad", ["ok"])
    c.check("坏文件之后仍然能写入", mc.get("llm", "after_bad") == ["ok"])

    leftovers = list(p.parent.glob(".models_*"))
    c.check("★ 原子写不残留临时文件", leftovers == [], str(leftovers))

    payload = json.loads(p.read_text(encoding="utf-8"))
    c.check("文件名是 model_cache.json", p.name == "model_cache.json")
    c.check("顶层按命名空间分组", "llm" in payload, str(list(payload)))


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_roundtrip(c)
        section_namespace(c)
        section_stale(c)
        section_robust(c)
    finally:
        print()
        print("=" * 78)
        print(f"模型清单缓存守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())