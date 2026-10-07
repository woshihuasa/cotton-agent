# -*- coding: utf-8 -*-
"""记忆冒烟守卫 —— 证明 L4 / L3 记忆链路"还活着"。

【定位】这**不是**记忆评测集（那是 E4，已推迟）。它只回答一个问题：
        **改动之后，记忆还能用吗？** —— 不回答"记忆判得好不好"。
        所以它必须是**确定性**的：不含 LLM 调用，只依赖 Embedding。

【覆盖】UserMemoryStore 的全部 8 个方法 + 冲突检索的命中/未命中判别：
        add_l4_memory / search_l4_for_conflict / update_l4_memory /
        delete_l4_memory / retrieve_l4_memory /
        add_session_summary / retrieve_session_summaries / clear_session_summaries

【不覆盖】LLM 驱动的 ADD / UPDATE / DELETE / NOOP 四分类判定
        （在 `rag_engine._start_memory_worker` 中，非确定性）→ 归 E4。

【隔离】全程在沙箱目录上运行，**绝不触碰生产记忆库**。
        沙箱通过"复制 fixture 后清空全部条目"得到——不新建 ChromaDB
        （受限环境下新建库会被拒，见 ROADMAP S0）。

【成本】约 10 次 Embedding 调用，**零 LLM 调用**。

【用法】python tests/smoke_memory.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from config import AppConfig                       # noqa: E402,F401  (仅用于路径自举)
from core.user_memory import UserMemoryStore       # noqa: E402

FIXTURE_DB = ROOT / "tests" / "eval_fixtures" / "memory" / "db"
SANDBOX = ROOT / "tests" / "eval_outputs" / "smoke_memory_sandbox"

# 集合名 → 实例属性名（两者不同名，显式映射避免 getattr 猜错）
COLLECTION_ATTRS = {
    "user_memory": "_memory_collection",
    "session_summaries": "_summary_collection",
}

# 测试用事实刻意与棉花无关，避免与 fixture 里的真实记忆语义撞车
FACT = "用户的测试标记是 ZX-7788"
FACT_PARAPHRASE = "用户说自己有个测试标记叫 ZX-7788"
FACT_UPDATED = "用户的测试标记已改为 ZX-9900"
UNRELATED = "今天天气不错，适合去湖边钓鱼"
SUMMARY = "本次测试对话中用户确认了测试标记与流程可用性。"
SESSION = "smoke-test-session"

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    _results.append((name, bool(cond), detail))
    print(f"    [{'OK ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def main() -> int:
    print("=" * 74)
    print("记忆冒烟守卫 —— L4 / L3 链路回归")
    print("=" * 74)

    if not FIXTURE_DB.exists():
        print(f"[致命] 未找到记忆 fixture: {FIXTURE_DB}")
        print("       请先运行: python tests/export_memory_fixture.py")
        return 2

    prod = UserMemoryStore()                       # 只读，用于对照
    prod_before = prod.counts()
    print(f"\n[0] 生产记忆库基线（只读对照）: {prod_before}")

    print(f"\n[1] 建沙箱并清空（复制 fixture → 删除全部条目）")
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)
    shutil.copytree(FIXTURE_DB, SANDBOX / "chroma_user_memory")

    st = UserMemoryStore(db_path=str(SANDBOX / "chroma_user_memory"))
    print(f"    沙箱载入 fixture: {st.counts()}")
    for name, attr in COLLECTION_ATTRS.items():
        col = getattr(st, attr)
        ids = col.get(include=[]).get("ids") or []
        if ids:
            col.delete(ids=ids)
    check("沙箱已清空", st.counts() == {"user_memory": 0, "session_summaries": 0},
          str(st.counts()))

    print(f"\n[2] L4 写入与读取")
    doc_id = st.add_l4_memory(FACT, "fact")
    check("add_l4_memory 返回非空 ID", bool(doc_id), doc_id[:8])
    check("条目数 +1", st.counts()["user_memory"] == 1)

    got = st.retrieve_l4_memory("测试标记")
    check("retrieve_l4_memory 能召回刚写入的事实", FACT in got, f"命中 {len(got)} 条")

    check("非法 category 回落为 fact",
          bool(st.add_l4_memory("用户的另一条测试事实", "不存在的类别")))

    print(f"\n[3] 冲突检索（ADD / NOOP 的确定性代理）")
    hit_id, hit_text = st.search_l4_for_conflict(FACT)
    check("同文本 → 命中（闸门会判 NOOP）", hit_id == doc_id and hit_text == FACT)

    p_id, _ = st.search_l4_for_conflict(FACT_PARAPHRASE)
    check("近义改写 → 命中（闸门会判 UPDATE/NOOP）", p_id is not None,
          "未命中——阈值偏严，非链路故障" if p_id is None else "")

    u_id, _ = st.search_l4_for_conflict(UNRELATED)
    check("无关文本 → 未命中（闸门会判 ADD）", u_id is None,
          "" if u_id is None else f"误命中 {u_id[:8]}")

    print(f"\n[4] L4 更新与删除")
    st.update_l4_memory(doc_id, FACT_UPDATED)
    got2 = st.retrieve_l4_memory("测试标记")
    check("update_l4_memory 生效", FACT_UPDATED in got2 and FACT not in got2)

    st.delete_l4_memory(doc_id)
    got3 = st.retrieve_l4_memory("测试标记")
    check("delete_l4_memory 生效", FACT_UPDATED not in got3)

    print(f"\n[5] L3 摘要链路")
    sid_doc = st.add_session_summary(SESSION, SUMMARY)
    check("add_session_summary 返回非空 ID", bool(sid_doc), sid_doc[:8])
    check("摘要条目数 +1", st.counts()["session_summaries"] == 1)

    rec = st.retrieve_session_summaries("测试标记 流程")
    check("retrieve_session_summaries 能召回", SUMMARY in rec, f"命中 {len(rec)} 条")

    st.clear_session_summaries(SESSION)
    check("clear_session_summaries 生效", st.counts()["session_summaries"] == 0)

    print(f"\n[6] 隔离性：生产记忆库是否被动过？")
    prod_after = prod.counts()
    check("生产条目数未变", prod_before == prod_after, f"{prod_before} → {prod_after}")

    shutil.rmtree(SANDBOX, ignore_errors=True)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print()
    print("=" * 74)
    print(f"冒烟守卫结果: {passed}/{total} 通过")
    failed = [n for n, ok, _ in _results if not ok]
    if failed:
        print("失败项:")
        for n in failed:
            print(f"  · {n}")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
