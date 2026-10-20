# -*- coding: utf-8 -*-
"""L4 作用域拆分守卫 —— 写入路由、跨库合并检索、旧库兼容。

【定位】`smoke_memory.py` 守的是"记忆链路还活着吗"（需 embedding，花钱）；
        本脚本守的是**拆分之后的新行为**，且**零 API 调用** ——
        办法是把 store 的 `_embeddings` 换成**确定性桩件**（字符袋向量），
        于是写入/检索/定位全程离线可跑。

【为什么必须零成本】拆分改的是记忆层最核心的路径，需要能**随手反复跑**。
        若每次都要付 embedding，它就会像 `smoke_memory` 一样变成"偶尔跑一次"的东西。

【覆盖】
        · 写入路由：画像式 → user_profile；含阶段锚点 → project_facts
        · 做法式降级：`用户吐絮期慎喷药` 换季成立 → 画像（不是项目事实）
        · `counts()` 形状**保持不变**（旧守卫做精确字典相等断言）
        · 跨库合并检索（拆分后漏掉另一半就会召回不全）
        · update / delete 自动定位所在库；update **保留** category metadata
        · 旧库兼容：只有 `user_memory` 的库（评测 fixture / 未迁移安装）
        · 判据一致性：`tests/classify_l4.py` 的原语必须与 `core.memory_scope` 相同

【用法】python tests/check_memory_split.py
"""
from __future__ import annotations

import inspect
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import chromadb                                            # noqa: E402
from core.memory_scope import (                            # noqa: E402
    PRACTICE,
    PROFILE_COLLECTION,
    PROJECT_COLLECTION,
    STAGE_TOKENS,
    STRONG_TIME,
    TIME_ANCHOR,
    classify_scope,
)
from core.rag_engine import RAGEngine                      # noqa: E402
from core.user_memory import UserMemoryStore               # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "memory_split_sandbox"


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


class _FakeEmbeddings:
    """确定性字符袋向量：同样的文本给同样的向量，全程零 API。

    只用来验证**管路**（路由/合并/定位），不验证检索质量 ——
    质量由 embedding 模型决定，与本次拆分无关。
    """

    DIM = 16

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.DIM
        for ch in text or "":
            v[ord(ch) % self.DIM] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts):                      # noqa: ANN001
        return [self._vec(t) for t in texts]

    def embed_query(self, text):                           # noqa: ANN001
        return self._vec(text)


def _store(name: str, **kw) -> UserMemoryStore:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    path = SANDBOX / name
    if path.exists():
        shutil.rmtree(path)
    st = UserMemoryStore(db_path=str(path), **kw)
    st._embeddings = _FakeEmbeddings()          # 桩件：零 API
    return st


PROFILE_FACT = "用户忌漫灌"
PROJECT_FACT = "用户棉田在8月31日停水"
PRACTICE_FACT = "用户吐絮期慎喷药并宜在傍晚打药"


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 部分：判据本身（纯函数，零依赖）")
    # ------------------------------------------------------------------
    c.check("稳定偏好 → 画像", classify_scope(PROFILE_FACT) == "profile")
    c.check("带时间/阶段锚点 → 项目事实", classify_scope(PROJECT_FACT) == "project")
    c.check("做法式且无强时间锚点 → 画像（换季仍成立）",
            classify_scope(PRACTICE_FACT) == "profile", classify_scope(PRACTICE_FACT))
    c.check("做法式但**有**强时间锚点 → 仍算项目事实",
            classify_scope("用户当前应停灌，旱情最多补≤10方/亩") == "project")
    c.check("空串 → 画像（兜底不抛异常）", classify_scope("") == "profile")

    # ------------------------------------------------------------------
    c.section("第 2 部分：写入路由（走真实写入路径）")
    # ------------------------------------------------------------------
    st = _store("routing")
    id_p = st.add_l4_memory(PROFILE_FACT, "preference")
    id_j = st.add_l4_memory(PROJECT_FACT, "fact")
    id_r = st.add_l4_memory(PRACTICE_FACT, "preference")

    split = st.counts_split()
    c.check("画像库收到 2 条（稳定偏好 + 做法式）",
            split.get(PROFILE_COLLECTION) == 2, str(split))
    c.check("项目事实库收到 1 条（含阶段锚点）",
            split.get(PROJECT_COLLECTION) == 1, str(split))
    c.check("两条落库确实是不同的集合",
            id_p != id_j and st._find_l4(id_p) is not st._find_l4(id_j))
    c.check("做法式落进了画像库", st._find_l4(id_r) is st._find_l4(id_p))

    # ------------------------------------------------------------------
    c.section("第 3 部分：counts() 形状不变（旧守卫做精确字典相等断言）")
    # ------------------------------------------------------------------
    c.check("counts() 键名与旧版一致",
            set(st.counts().keys()) == {"user_memory", "session_summaries"},
            str(st.counts()))
    c.check("counts()['user_memory'] = 两库之和",
            st.counts()["user_memory"] == 3, str(st.counts()))
    empty = _store("shape")
    c.check("空库 counts() 精确等于旧形状",
            empty.counts() == {"user_memory": 0, "session_summaries": 0},
            str(empty.counts()))

    # ------------------------------------------------------------------
    c.section("第 4 部分：跨库合并检索（拆分后最易漏的一处）")
    # ------------------------------------------------------------------
    st = _store("merge")
    st.add_l4_memory(PROFILE_FACT, "preference")
    st.add_l4_memory(PROJECT_FACT, "fact")
    got = st.retrieve_l4_memory(PROFILE_FACT, k=2)
    c.check("查询能同时看到两个库的内容", len(got) == 2, str(got))
    c.check("最像的排第一（距离升序）", got and got[0] == PROFILE_FACT, str(got))
    only1 = st.retrieve_l4_memory(PROFILE_FACT, k=1)
    c.check("k=1 时只回 1 条", len(only1) == 1, str(only1))

    # ------------------------------------------------------------------
    c.section("第 5 部分：update / delete 自动定位所在库")
    # ------------------------------------------------------------------
    st = _store("locate")
    pid = st.add_l4_memory(PROFILE_FACT, "preference")
    jid = st.add_l4_memory(PROJECT_FACT, "fact")

    st.update_l4_memory(jid, "用户棉田已停水")
    c.check("更新项目事实后仍在该库（条数守恒）",
            st.counts_split().get(PROJECT_COLLECTION) == 1, str(st.counts_split()))
    c.check("更新后的文本已生效",
            "已停水" in (st.retrieve_l4_memory("用户棉田已停水", k=1) or [""])[0],
            str(st.retrieve_l4_memory("用户棉田已停水", k=1)))

    md = st._find_l4(jid).get(ids=[jid], include=["metadatas"])["metadatas"][0]
    c.check("更新**保留**了 category（旧实现只写 updated_at 会丢掉它）",
            md.get("category") == "fact", str(md))

    st.delete_l4_memory(pid)
    c.check("删除画像事实后画像库少 1 条",
            st.counts_split().get(PROFILE_COLLECTION) == 0, str(st.counts_split()))
    c.check("删错库不会发生（项目事实仍在）",
            st.counts_split().get(PROJECT_COLLECTION) == 1, str(st.counts_split()))
    st.delete_l4_memory("不存在的-id")
    c.check("删除不存在的 id 不抛异常", True)

    # ------------------------------------------------------------------
    c.section("第 6 部分：clear_all_l4 跨库清空（守卫不再摸私有属性）")
    # ------------------------------------------------------------------
    st = _store("clear")
    st.add_l4_memory(PROFILE_FACT, "preference")
    st.add_l4_memory(PROJECT_FACT, "fact")
    st.add_session_summary("s1", "一段摘要")
    removed = st.clear_all_l4()
    c.check("clear_all_l4 报告删除 2 条", removed == 2, str(removed))
    c.check("两个 L4 库都已空", st.counts()["user_memory"] == 0, str(st.counts()))
    c.check("L3 未被误清", st.counts()["session_summaries"] == 1, str(st.counts()))
    c.check("clear_all_summaries 清掉 L3", st.clear_all_summaries() == 1)

    # ------------------------------------------------------------------
    c.section("第 7 部分：旧库兼容（评测 fixture / 未迁移安装）")
    # ------------------------------------------------------------------
    legacy_path = SANDBOX / "legacy"
    if legacy_path.exists():
        shutil.rmtree(legacy_path)
    legacy_path.mkdir(parents=True)
    cli = chromadb.PersistentClient(path=str(legacy_path))
    old = cli.get_or_create_collection("user_memory")
    old.add(ids=["legacy-1"], embeddings=[[0.5] * 16], documents=["用户旧库里的记忆"])

    st = UserMemoryStore(db_path=str(legacy_path))
    st._embeddings = _FakeEmbeddings()
    c.check("检测到旧库模式（两新库为空 + 旧库有数据）",
            st._legacy_collection is not None)
    c.check("旧库模式 counts() 反映旧库", st.counts()["user_memory"] == 1, str(st.counts()))
    c.check("旧库模式 counts_split() 标出 legacy",
            any("legacy" in k for k in st.counts_split()), str(st.counts_split()))
    st.add_l4_memory(PROJECT_FACT, "fact")
    c.check("旧库模式下写入仍落旧库（不强行拆分）",
            st.counts_split().get("legacy(user_memory)") == 2, str(st.counts_split()))

    # 生产形态：两新库非空 → 不进旧库模式
    st2 = _store("migrated")
    st2.add_l4_memory(PROFILE_FACT, "preference")
    c.check("两新库非空时不启用旧库模式（生产形态）",
            st2._legacy_collection is None)

    # ------------------------------------------------------------------
    c.section("第 8 部分：判据一致性（防'两份真相'）")
    # ------------------------------------------------------------------
    import classify_l4 as C                                # noqa: E402
    c.check("STAGE_TOKENS 与治理脚本一致", tuple(C.STAGE_TOKENS) == tuple(STAGE_TOKENS))
    c.check("TIME_ANCHOR 与治理脚本一致", C.TIME_ANCHOR.pattern == TIME_ANCHOR.pattern)
    c.check("PRACTICE 与治理脚本一致", C.PRACTICE.pattern == PRACTICE.pattern)
    c.check("STRONG_TIME 与治理脚本一致", C.STRONG_TIME.pattern == STRONG_TIME.pattern)

    # ------------------------------------------------------------------
    c.section("第 9 部分：asserted_at（断言时间 —— 冲突消解的排序信号）")
    # ------------------------------------------------------------------
    st = _store("asserted")
    aid = st.add_l4_memory(PROFILE_FACT, "preference")
    md = st._find_l4(aid).get(ids=[aid], include=["metadatas"])["metadatas"][0]
    c.check("写入即记录 asserted_at", bool(md.get("asserted_at")), str(md))
    c.check("来源标为 write_time",
            md.get("asserted_at_source") == "write_time", str(md))
    first = str(md.get("asserted_at"))

    st.update_l4_memory(aid, "用户非常忌漫灌")
    md2 = st._find_l4(aid).get(ids=[aid], include=["metadatas"])["metadatas"][0]
    c.check("更新**不改写** asserted_at（否则归并过的旧事实会显得很新）",
            str(md2.get("asserted_at")) == first,
            f"{first} → {md2.get('asserted_at')}")
    c.check("更新后 updated_at 仍在", bool(md2.get("updated_at")), str(md2))
    c.check("asserted_at() 取得到", st.asserted_at(aid) == first, st.asserted_at(aid))
    c.check("asserted_at() 对不存在的 id 返回空串", st.asserted_at("不存在") == "")

    # 缺字段的旧条目：更新时用 updated_at 近似回填并标注来源
    col = st._find_l4(aid)
    col.add(ids=["no-asserted"], embeddings=[[0.1] * 16],
            documents=["用户旧条目"],
            metadatas=[{"updated_at": "2026-08-08T00:00:00+00:00", "category": "fact"}])
    st.update_l4_memory("no-asserted", "用户旧条目已修改")
    md3 = col.get(ids=["no-asserted"], include=["metadatas"])["metadatas"][0]
    c.check("缺失时用 updated_at 近似回填并标注 backfill_approx",
            md3.get("asserted_at") == "2026-08-08T00:00:00+00:00"
            and md3.get("asserted_at_source") == "backfill_approx", str(md3))
    c.check("近似回填不会覆盖已有的 asserted_at",
            col.get(ids=[aid], include=["metadatas"])["metadatas"][0].get("asserted_at_source")
            == "write_time")

    # ------------------------------------------------------------------
    c.section("第 10 部分：冲突消解提示词带上了时间（零 LLM：桩件捕获 prompt）")
    # ------------------------------------------------------------------
    eng_path = SANDBOX / "prompt_time"
    if eng_path.exists():
        shutil.rmtree(eng_path)
    eng = RAGEngine(user_data_dir=eng_path)
    eng.memory._embeddings = _FakeEmbeddings()      # 零 API

    eng.memory.add_l4_memory("用户主栽塔河2号", "fact")
    eng.memory.add_l4_memory("用户有100亩棉田", "fact")

    captured: dict[str, str] = {}

    def _fake_gen(messages):                       # noqa: ANN001
        captured["prompt"] = messages[0]["content"]
        return "NOOP"

    eng.llm.generate_response = _fake_gen          # type: ignore[method-assign]
    eng.resolve_memory_fact("用户已改种新陆早77号", "fact")

    p = captured.get("prompt", "")
    c.check("冲突消解真的发出了 prompt（桩件捕获到）", bool(p))
    c.check("候选列表带上了记录日期 YYYY-MM-DD",
            bool(re.search(r"（\d{4}-\d{2}-\d{2}）", p)), p[-320:])
    c.check("提示词含「计划不得据时间覆盖事实」这条风险约束",
            "计划不得据时间覆盖事实" in p)
    c.check("提示词声明时间是参考信息、不是判据", "不是判据" in p)
    c.check("缺时间时不留空括号", "（）" not in p)
    c.check("两个候选都带了日期（不是只给第一个加）",
            len(re.findall(r"（\d{4}-\d{2}-\d{2}）", p)) == 2,
            str(re.findall(r"（\d{4}-\d{2}-\d{2}）", p)))

    # ------------------------------------------------------------------
    c.section("第 11 部分：接口一致性与 Web 短路（三条历史缺陷的守卫）")
    # ------------------------------------------------------------------
    # 这三条都是"约定没有守卫"导致的：# ①接口一致、③默认值一致 都只是 docstring 里的
    # 承诺；# ②的注释承诺了省访客 LLM，但实现的只是"不落库"。修完之后必须留下断言，
    # 否则下一次重构会把它们再打破一遍。
    from core.user_memory import NullMemoryStore

    real_pub = {n for n in dir(UserMemoryStore) if not n.startswith("_")}
    null_pub = {n for n in dir(NullMemoryStore) if not n.startswith("_")}
    c.check("★ 两个 store 的**公开方法集完全相等**（新增接口必须同步补 Null 版）",
            real_pub == null_pub,
            f"缺 {sorted(real_pub - null_pub)} / 多 {sorted(null_pub - real_pub)}")
    c.check("NullMemoryStore 有 search_l4_candidates（曾长期缺失，Web 下每次抛 AttributeError）",
            hasattr(NullMemoryStore, "search_l4_candidates"))
    c.check("NullMemoryStore.search_l4_candidates 返回空列表",
            NullMemoryStore().search_l4_candidates("任意事实") == [])

    _sr = inspect.signature(UserMemoryStore.search_l4_for_conflict)
    _sn = inspect.signature(NullMemoryStore.search_l4_for_conflict)
    c.check("★ search_l4_for_conflict 默认阈值两处一致（曾 0.7 vs 0.79）",
            _sr.parameters["threshold"].default == _sn.parameters["threshold"].default,
            f"{_sr.parameters['threshold'].default} vs {_sn.parameters['threshold'].default}")

    # ② Web 短路：**双向断言** —— 既证明守卫生效，也证明它没把功能一起关掉
    web_engine = RAGEngine(owns_user_data=False)
    web_engine.create_session()
    web_hits = {"n": 0}
    web_engine.llm.generate_response = lambda m: (          # type: ignore[method-assign]
        web_hits.__setitem__("n", web_hits["n"] + 1), "[]")[1]
    web_engine._start_memory_worker("今天适合打药吗", "适合")
    time.sleep(0.3)
    c.check("★ Web 引擎（不拥有用户数据）**不发起 L4 抽取调用**",
            web_hits["n"] == 0, f"实际发了 {web_hits['n']} 次")

    own_engine = RAGEngine(user_data_dir=SANDBOX / "worker_probe")
    own_engine.create_session()
    own_hits = {"n": 0}
    own_engine.llm.generate_response = lambda m: (          # type: ignore[method-assign]
        own_hits.__setitem__("n", own_hits["n"] + 1), "[]")[1]
    own_engine._start_memory_worker("今天适合打药吗", "适合")
    time.sleep(0.6)
    c.check("拥有用户数据的引擎**仍会**发起抽取（守卫没有误关功能）",
            own_hits["n"] == 1, f"实际发了 {own_hits['n']} 次")

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"L4 作用域拆分守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())