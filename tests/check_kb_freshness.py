# -*- coding: utf-8 -*-
"""知识库时效性守卫 —— 守住"切片带年份、检索带出处、同主题取最新"。

【它守的是一个真实缺陷（2026-10-18 发现并修复）】

    知识库里同一主题存了多个年份版本：

        棉花科学灌溉技术指导意见  → 2023 / 2024 / 2025 / 2026
        棉花春季生产技术指导意见  → 2025 / 2026 / 无年份
        棉花科学施肥指导意见      → 2025 / 2026

    而年度指导意见**内容逐年高度重复**（实测 2023 与 2025 的花铃期段落**逐字相同**）。
    于是向量检索会同时命中多个年份，且**无法区分**。

    更关键的是：原来的 `retrieve_context()` 最后一行是
    `contexts = [doc.page_content for doc in docs]` —— **把 source 整个丢掉**。
    模型根本看不到自己引用的是哪一年的文件，**时效性就此消失**。

【本守卫的三层】

    ① **年份解析**：文件名 → 年份（含"老切片只有 source 也能解析"的回退）
    ② **同主题去重**：多版本同时命中时只留最新；**无年份的不参与比较、不被淘汰**
    ③ **端到端**：检索返回的上下文里**真的出现年份与出处**

【零 LLM / 零网络】只用已建好的本地向量库；若库为空则跳过端到端部分并说明。

【用法】python tests/check_kb_freshness.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.knowledge_base import (                              # noqa: E402
    _dedupe_by_topic, _format_chunk_with_source, _topic_key,
    chunk_year, doc_year,
)


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


class Doc:
    """轻量替身：只要有 `page_content` 与 `metadata` 就够。"""

    def __init__(self, content: str, **metadata) -> None:
        self.page_content = content
        self.metadata = metadata


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：★ 年份解析（只看文件名，不看正文）")
    # ------------------------------------------------------------------
    cases = [
        ("data/新疆_栽培与水肥/2026年棉花科学灌溉技术指导意见.md", 2026),
        ("data/新疆_栽培与水肥/2023年棉花科学灌溉技术指导意见.md", 2023),
        ("E:\\cotton_agent\\data\\新疆_病虫害防治\\2024年棉花重大病虫害防控技术方案.md", 2024),
        ("data/新疆_栽培与水肥/棉花应对高温天气生产技术指导意见.md", None),
        ("data/新疆_数据统计/新疆棉花产量年度统计.md", None),
        ("2025年棉花后期生产技术指导意见.md", 2025),
    ]
    for src, want in cases:
        got = doc_year(src)
        c.check(f"★ {Path(src.replace(chr(92), '/')).name[:34]:<36} → {want}",
                got == want, f"实际 {got}")

    c.check("★★ 反斜杠路径也能解析（Windows 路径）",
            doc_year("E:\\a\\b\\2024年某文件.md") == 2024)
    c.check("★ 解析不到不猜（返回 None 而不是 0）",
            doc_year("随便一个名字.md") is None)
    c.check("★ 空输入不抛异常", doc_year("") is None)

    # ------------------------------------------------------------------
    c.section("第 2 节：★★ 老切片（只有 source、无 year 字段）必须也能拿到年份")
    # ------------------------------------------------------------------
    # 这一条很重要：索引里已有的 152 个切片**都没有 year 字段**，
    # 有了回退就不必重建索引（重建要花 Embedding 费用）。
    c.check("★★ metadata 无 year 时从 source 现解析",
            chunk_year({"source": "data/x/2025年棉花科学施肥指导意见.md"}) == 2025)
    c.check("★ metadata 有 year 时优先用它（显式值优先）",
            chunk_year({"year": 2027, "source": "data/x/2020年a.md"}) == 2027)
    c.check("★ 两者都没有 → None", chunk_year({}) is None)
    c.check("★ metadata 为 None 不抛异常", chunk_year(None) is None)

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 同主题分组")
    # ------------------------------------------------------------------
    c.check("★ 同年份不同主题 → 不同组",
            _topic_key("data/d/2026年A技术.md") != _topic_key("data/d/2026年B技术.md"))
    c.check("★★ 同主题不同年份 → **同一组**",
            _topic_key("data/d/2023年A技术.md") == _topic_key("data/d/2026年A技术.md"))
    c.check("★ 不同目录同名主题 → 不同组（目录参与分组）",
            _topic_key("data/d1/2026年A技术.md") != _topic_key("data/d2/2026年A技术.md"))
    c.check("★ 分组键里的主题名已去掉年份",
            "2026" not in _topic_key("data/d/2026年A技术.md")[1],
            _topic_key("data/d/2026年A技术.md")[1])

    # ------------------------------------------------------------------
    c.section("第 4 节：★★ 同主题去重 —— 只留最新，且不误伤")
    # ------------------------------------------------------------------
    docs = [
        Doc("旧版正文", source="data/d/2023年灌溉技术.md"),
        Doc("其他主题", source="data/d/2025年施肥技术.md"),
        Doc("新版正文", source="data/d/2026年灌溉技术.md"),
        Doc("无年份资料", source="data/d/棉花应对高温.md"),
    ]
    kept = _dedupe_by_topic(docs)
    kept_src = [Path(d.metadata["source"]).name for d in kept]

    c.check("★★ 同主题只留最新年份（2026 保留、2023 淘汰）",
            "2026年灌溉技术.md" in kept_src and "2023年灌溉技术.md" not in kept_src,
            str(kept_src))
    c.check("★★ **无年份的切片不被淘汰**（不猜年份，也不能误删）",
            "棉花应对高温.md" in kept_src, str(kept_src))
    c.check("★ 其他主题不受影响", "2025年施肥技术.md" in kept_src, str(kept_src))
    c.check("★★ 保留原顺序（不动跨主题的相关度排序）",
            kept_src == ["其他主题" if False else "2025年施肥技术.md",
                         "2026年灌溉技术.md", "棉花应对高温.md"],
            str(kept_src))

    # 全无年份时不去重
    only_none = [Doc("a", source="data/d/甲.md"), Doc("b", source="data/d/乙.md")]
    c.check("★ 全无年份 → 一条都不删", len(_dedupe_by_topic(only_none)) == 2)
    c.check("★ 空列表不抛异常", _dedupe_by_topic([]) == [])

    # 只有一版时不删
    single = [Doc("x", source="data/d/2026年灌溉技术.md")]
    c.check("★ 同主题只有一版 → 保留", len(_dedupe_by_topic(single)) == 1)

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ 上下文里**真的**带上出处与年份")
    # ------------------------------------------------------------------
    out = _format_chunk_with_source(
        Doc("正文内容", source="data/新疆_栽培与水肥/2026年棉花科学灌溉技术指导意见.md"))
    c.check("★★ 带回出处（文件名）", "2026年棉花科学灌溉技术指导意见.md" in out, out[:80])
    c.check("★★ 带回年份（单独可读）", "年份：2026" in out, out[:80])
    c.check("★ 保留正文", "正文内容" in out)
    c.check("★ 格式有〔出处：… ｜ 年份：…〕标记",
            bool(re.search(r"〔出处：.+[｜|]\s*年份：.+〕", out)), out[:60])

    out2 = _format_chunk_with_source(
        Doc("x", source="data/d/棉花应对高温天气生产技术指导意见.md"))
    c.check("★★ 无年份时标「未标注」（不假装是最新的）",
            "年份：未标注" in out2, out2[:80])

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    c.section("第 6 节：★★ source 多写法防御（防「只删掉一半」）")
    # ------------------------------------------------------------------
    # 背景：索引里 25 个文件中有 20 个存的是**绝对路径**、5 个是**相对标识**
    # （相对标识是后来改造引入的，旧切片没重嵌）。
    # _indexed_files() 原本是 out[归一化] = (原始source, hash) —— **后写覆盖先写**，
    # 于是同一文件若有多种写法，删除时**只删得掉一种，另一种永远残留**。
    from core.knowledge_base import KnowledgeBase as _KB
    kb2 = _KB.__new__(_KB)                      # 不触发真实模型加载
    kb2._source_aliases = {
        "新疆_数据统计/甲.md": {
            "E:\\cotton_agent\\data\\新疆_数据统计\\甲.md",
            "data/新疆_数据统计/甲.md",
        }
    }
    variants = kb2.source_variants("新疆_数据统计/甲.md")
    c.check("★★ 同一文件的**两种写法都被展开**", len(variants) == 2, str(variants))
    c.check("★ 展开结果含绝对路径写法",
            any(v.startswith("E:") for v in variants), str(variants))
    c.check("★ 展开结果含相对标识写法",
            any(v.startswith("data/") for v in variants), str(variants))

    kb3 = _KB.__new__(_KB)
    kb3._source_aliases = {}
    v3 = kb3.source_variants("a/b.md")
    c.check("★ 无别名记录 → 退回相对标识（**不为空**，否则删除会静默无效）",
            bool(v3) and v3[0].startswith("data/"), str(v3))

    c.section("第 7 节：端到端 —— 真实向量库")
    # ------------------------------------------------------------------
    try:
        from core.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        n_total = len((kb._vector_store.get(include=["metadatas"]).get("metadatas") or []))
    except Exception as e:                                     # noqa: BLE001
        n_total = 0
        c.check("加载本地向量库", False, f"{type(e).__name__}: {e}")

    if n_total == 0:
        c.check("★ 向量库为空 → 跳过端到端（并明确说明，不假装通过）", True)
        print("     （本地无索引，端到端部分跳过）")
    else:
        c.check(f"★ 向量库可用（{n_total} 个切片）", n_total > 0)
        q = "棉花花铃期应该怎么灌溉，浇多少水"
        new_ctx = kb.retrieve_context(q, k=3)
        old_ctx = kb.retrieve_context(q, k=3, with_source=False, prefer_latest=False)

        c.check("★★ 新行为：上下文里**出现年份**",
                bool(re.search(r"年份：20\d\d", new_ctx)), new_ctx[:100])
        c.check("★★ 新行为：上下文里**出现出处标记**", new_ctx.count("〔出处：") >= 1)
        c.check("★★ 老行为（对照）：**看不到年份**（这正是被修的缺陷）",
                not re.search(r"年份：", old_ctx), old_ctx[:80])

        # ★ 同主题（去年份后）只应出现一次 —— 这才是"去重真的生效"的可判定检查。
        # （我一开始在这里写了 `... or True`，那是个**永远通过的假断言**，已删掉。
        #   写不出可判定的断言时，宁可不写，也不能写一个恒真的。）
        topics = re.findall(r"出处：(.+?)〕", new_ctx)
        stems = [re.sub(r"20\d\d年", "", Path(t.strip()).stem) for t in topics]
        c.check("★★ 同主题只出现一次（去重真的生效）",
                len(stems) == len(set(stems)), str(stems))
        c.check("★ 去重后段数不超 k", len(topics) <= 3, str(len(topics)))

    print()
    print("=" * 78)
    print(f"知识库时效性守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())