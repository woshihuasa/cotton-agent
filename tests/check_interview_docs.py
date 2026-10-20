# -*- coding: utf-8 -*-
"""面试文档改写后的质量核验（回写前必跑）。

【为什么需要】10 份文档由 5 个子代理并行改写。并行改写的风险不是"某一份没改好"，
        而是 **"某一份悄悄丢了技术内容"** —— 而且改的人自己不会报告这件事。

        所以回写前必须做一次**机械核验**，逐项检查规范里的红线：
        · 每节都有「一句话结论」吗
        · 原有的关键数字、章节标题、代码路径**少了没有**
        · 有没有人为了"通俗"把缺陷清单删掉

【用法】python tests/check_interview_docs.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEW = ROOT / "tests" / "eval_outputs" / "interview"
OLD = Path("D:/面试资料")          # 原始版本（对照基线）

#: 每份文档必须**保留**的关键字符串（数字、术语、路径）—— 从原文档抽取
MUST_KEEP = {
    "01_评测体系建设与报告.md": [
        "92.6", "66.7", "MRR", "Hit@1", "Hit@20", "96.7", "100%",
        "忠实度", "要点覆盖", "拒答", "judge", "20015",
    ],
    "02_RAG 检索链路.md": [
        "Hit@3", "Hit@20", "RRF", "Rerank", "bge-reranker-v2-m3",
        "BM25", "top-3", "ChromaDB",
    ],
    "03_服务化与工具暴露.md": [
        "FastAPI", "SSE", "MCP", "stdio", "限流", "会话池",
    ],
    "04_四层记忆架构与数据隔离.md": [
        "L1", "L2", "L3", "L4", "ChromaDB", "asserted_at",
        "NullMemoryStore", "要素闸", "S0", "S7", "冲突消解",
    ],
    "05_工程化与交付.md": [
        "PyInstaller", "onedir", "白名单", "自动更新", "降级",
    ],
    "06_技术选型与决策.md": [
        "候选", "判据", "结论", "代价", "诚实清单",
    ],
    "07_工具层功能与实现.md": [
        "get_weather", "web_search", "query_cotton_stats",
        "calc_price_volatility", "plot_trend", "analyze_yield",
        "ToolResult", "_materialize", "缺陷",
    ],
    "08_提示词设计与迭代.md": [
        "SYSTEM_PROMPT", "REWRITE_SYSTEM_PROMPT", "L4_CONFLICT_PROMPT",
        "JUDGE_PROMPT", "缺陷",
    ],
    "09_UI与交互实现.md": [
        "PyQt6", "QThread", "matplotlib", "主线程", "流式", "缺陷",
    ],
    "00_项目总览.md": [
        "92.6", "检索集", "短板", "文档索引",
    ],
}

#: 不许出现的小孩腔（规范第五条红线）
BABY_TALK = ("小朋友", "其实很简单哦", "我们一起来学", "宝宝", "很简单对不对")


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0
        self.fails: list[str] = []

    def check(self, desc: str, cond: object, detail: str = "") -> None:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        if not ok:
            self.fails.append(desc + (f"   ← {detail}" if detail else ""))
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))


def main() -> int:
    c = Checker()
    print("=" * 78)
    print("面试文档改写核验（回写前必跑）")
    print("=" * 78)

    news = sorted(NEW.glob("*.md"))
    print(f"\n工作区待核验文档: {len(news)} 份\n")

    for f in news:
        name = f.name
        text = f.read_text("utf-8")
        lines = text.splitlines()
        print(f"--- {name} ---")

        # ① 有没有内容（不是空文件）
        c.check("非空", len(text) > 2000, f"{len(text)} 字")

        # ② 每份都要有「一句话结论」
        n_concl = text.count("一句话结论")
        c.check("含「一句话结论」", n_concl >= 1, f"{n_concl} 处")

        # ③ 一级标题都在（与原文档对照 —— 防"改写时删掉整节"）
        # ⚠️ 例外：`06` 号按计划**改了章节编号**（诚实清单 七→八，因新增了一节），
        # 所以只比"标题的文字部分"，不比编号。这是本轮唯一允许改标题的情况。
        old_path = OLD / name
        if old_path.exists():
            def _titles(t: str) -> set[str]:
                return {re.sub(r"^##\s*[〇一二三四五六七八九十\d]*[、.]?\s*", "", m.group(0)).strip()
                        for m in re.finditer(r"^## .+$", t, re.M)}
            missing = _titles(old_path.read_text("utf-8")) - _titles(text)
            c.check(f"一级标题未丢失（原 {len(_titles(old_path.read_text('utf-8')))} 个）",
                    not missing, f"丢了 {sorted(missing)[:3]}")

        # ④ ★ 关键数字/术语必须保留（防"为了通俗把数字删了"）
        keep = MUST_KEEP.get(name, [])
        if keep:
            lost = [k for k in keep if k not in text]
            c.check(f"★ 关键数字/术语保留（{len(keep)} 项）", not lost, f"丢了 {lost}")

        # ⑤ 篇幅没缩水（改写只该加解释，不该删内容）
        if old_path.exists():
            old_n = len(old_path.read_text("utf-8"))
            ratio = len(text) / max(1, old_n)
            c.check("篇幅未缩水（≥原 90%）", ratio >= 0.9, f"比值 {ratio:.2f}")

        # ⑥ 红线：不许出现小孩腔
        baby = [w for w in BABY_TALK if w in text]
        c.check("无小孩腔（规范红线）", not baby, str(baby))

        # ⑦ 代码块语法标记成对（防改写时破坏代码块）
        c.check("代码块围栏成偶数", text.count("```") % 2 == 0,
                f"{text.count('```')} 个")

        print()

    print("=" * 78)
    print(f"核验结果: {c.passed}/{c.total} 通过")
    if c.fails:
        print("\n未通过项：")
        for x in c.fails:
            print(f"  · {x}")
    print("=" * 78)
    return 0 if not c.fails else 1


if __name__ == "__main__":
    raise SystemExit(main())