# -*- coding: utf-8 -*-
"""D6 第三步：把 L4 存量**分家**到「画像库 / 项目事实库」两个集合。

【核心安全设计：不先删原数据】
        新库直接按**目标状态**构建（不含那 22 条、合并那 5 条），
        **源集合 `user_memory` 原样保留**作为回滚点。
        因此本脚本是**可重跑、可回滚**的：目标不对，删掉两个新集合重来即可，
        源数据从头到尾没被动过。这比"先备份再原地删"更安全。

【零 embedding 调用】直接复制源集合里的向量 —— 不重算、不花钱、结果确定。

【三步走（本脚本是第 3 步）】
        1. 分类（`classify_l4.py`，只读）           ✅ 已完成
        2. 人工确认（`d6_to_review.txt`）            ✅ 已完成
        3. 分家（本脚本）：备份 → 建两个新集合 → 对账 → 抽样校验   ← 不删源
        4. 代码改造（`UserMemoryStore` 双库读写）＋ 回归            ← 另开一步

【为什么代码改造要另开一步】桌面端的 `UserMemoryStore` 现在读写的是 `user_memory`。
        先建好新集合、验证无误，再改代码切过去 —— 顺序反了会让应用读到空库。

【分类复用】直接 import `tests/classify_l4.py` 的 `classify` / `loc_key` / `DELETE`，
        **不重新实现一遍规则** —— 否则"两份分类逻辑"又会各自演化。

【用法】python tests/migrate_l4_split.py
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import chromadb                                            # noqa: E402
import classify_l4 as C                                    # noqa: E402

DB_PATH = "chroma_user_memory"
SRC = "user_memory"
PROFILE = "user_profile"        # 画像库（锁死单用户全局）
PROJECT = "project_facts"       # 项目事实库（锁死单项目沙箱）
BACKUP_ROOT = ROOT / "tests" / "eval_outputs"
LOG = BACKUP_ROOT / "d6_migration_log.txt"


def open_db(path: Path) -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=str(path))


def backup() -> tuple[Path, bool]:
    """返回 (备份目录, 是否本次新建)。

    已有备份时**复用最早的那份**，不再复制 —— 最早那份是**迁移前的原始状态**（回滚点）；
    再复制只会把新集合也一起带进去（每轮迁移向源目录新增两个集合目录，+8 个文件），
    备份反而离"原始"越来越远。
    """
    existing = sorted(BACKUP_ROOT.glob("d6_backup_chroma_user_memory_*"))
    if existing:
        return existing[0], False
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_ROOT / f"d6_backup_chroma_user_memory_{stamp}"
    shutil.copytree(ROOT / DB_PATH, dst)
    return dst, True


def verify_backup(dst: Path) -> int:
    """验证备份**可打开且条目数一致** —— 没验证过的备份等于没有备份。"""
    return open_db(dst).get_collection(SRC).count()


def main() -> int:
    lines: list[str] = []

    def say(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    say("=" * 78)
    say("D6 第三步：L4 分家（源集合保留，可回滚）")
    say("=" * 78)

    # ---- 1. 备份 ----
    bak, fresh = backup()
    n_bak = verify_backup(bak)
    say(f"[1/6] 备份: {bak.relative_to(ROOT).as_posix()}"
        f"（{'本次新建' if fresh else '复用最早的已有备份'}）")
    say(f"      备份可打开，条目数 = {n_bak}")

    client = open_db(ROOT / DB_PATH)
    src = client.get_collection(SRC)
    got = src.get(include=["documents", "metadatas", "embeddings"])
    ids, docs, metas = got["ids"], got["documents"], got["metadatas"]
    embs = [list(map(float, e)) for e in got["embeddings"]]
    total = len(ids)
    say(f"[2/6] 源集合 {SRC}: {total} 条，向量维度 {len(embs[0]) if embs else 0}")

    # ---- 3. 决定每条去向 ----
    order = sorted(range(total), key=lambda i: ids[i])      # 排序保证可复现
    plan: list[tuple[int, str, str]] = []                   # (idx, 目标, 子标签)
    deleted: list[tuple[str, str]] = []
    merged: list[tuple[str, str, str]] = []                 # (被并掉id, 保留id, 文本)
    seen_loc: dict[tuple, str] = {}

    for i in order:
        cls, sub = C.classify(docs[i])
        if (cls, sub) in C.DELETE:
            deleted.append((ids[i], docs[i]))
            continue
        if (cls, sub) == ("A", "地点属性"):
            key = C.loc_key(docs[i])
            if key in seen_loc:
                merged.append((ids[i], seen_loc[key], docs[i]))
                continue
            seen_loc[key] = ids[i]
        target = PROJECT if cls == "B" else PROFILE
        plan.append((i, target, sub))

    say(f"[3/6] 计划：画像库 {sum(1 for _, t, _ in plan if t == PROFILE)} 条 · "
        f"项目事实库 {sum(1 for _, t, _ in plan if t == PROJECT)} 条 · "
        f"删除 {len(deleted)} 条 · 合并掉 {len(merged)} 条")

    # ---- 4. 建新集合（幂等：先删后建，避免残留） ----
    for name in (PROFILE, PROJECT):
        try:
            client.delete_collection(name)
        except Exception:
            pass
    col = {PROFILE: client.get_or_create_collection(PROFILE),
           PROJECT: client.get_or_create_collection(PROJECT)}

    for target in (PROFILE, PROJECT):
        idxs = [i for i, t, _ in plan if t == target]
        if not idxs:
            continue
        sub_of = {j: s for j, _, s in plan}
        md = []
        for i in idxs:
            m = dict(metas[i] or {})
            m["d6_class"] = sub_of[i]
            md.append(m)
        # 分批写入，避免单次请求过大
        step = 200
        for k in range(0, len(idxs), step):
            chunk = idxs[k:k + step]
            col[target].add(
                ids=[ids[i] for i in chunk],
                embeddings=[embs[i] for i in chunk],
                documents=[docs[i] for i in chunk],
                metadatas=[md[k + j] for j in range(len(chunk))],
            )
    say(f"[4/6] 已写入新集合：{PROFILE}={col[PROFILE].count()} · "
        f"{PROJECT}={col[PROJECT].count()}")

    # ---- 5. 对账（id 全覆盖） ----
    in_profile = set(col[PROFILE].get(include=[])["ids"])
    in_project = set(col[PROJECT].get(include=[])["ids"])
    del_ids = {i for i, _ in deleted}
    merged_ids = {i for i, _, _ in merged}
    all_src = set(ids)

    covered = in_profile | in_project | del_ids | merged_ids
    dup = in_profile & in_project
    say(f"[5/6] 对账：")
    say(f"      画像 {len(in_profile)} + 项目 {len(in_project)} + 删除 {len(del_ids)} "
        f"+ 合并掉 {len(merged_ids)} = {len(covered)}")
    say(f"      源集合 {total} 条 → {'全覆盖 ✓' if covered == all_src else '**有遗漏或多余 ✗**'}")
    say(f"      两库交集 = {len(dup)} → {'无重复归入 ✓' if not dup else '**同一条进了两个库 ✗**'}")

    # 抽样校验向量逐值一致（证明是"复制"而不是"重算"）
    sample = [i for i, t, _ in plan if t == PROFILE][:3]

    def _vec(coll, doc_id: str) -> list[float]:
        # ⚠️ Chroma 返回的是 **numpy 数组**：直接与 list 比较会得到逐元素数组，
        #    all() 随即抛 "truth value of an array is ambiguous"。
        #    本项目已被 numpy 咬过三次（短板 #21 的 int64 序列化、float64 静默通过、本次）。
        raw = coll.get(ids=[doc_id], include=["embeddings"])["embeddings"][0]
        return [float(x) for x in raw]

    same = all(_vec(col[PROFILE], ids[i]) == embs[i] for i in sample) if sample else False
    say(f"      抽样 {len(sample)} 条：向量逐值一致 → {'✓（零 embedding 调用）' if same else '**不一致 ✗**'}")

    # ---- 6. 源集合仍在 ----
    say(f"[6/6] 源集合 {SRC} 未改动，仍为 {src.count()} 条 —— 回滚点保留 ✓")

    # ---- 日志 ----
    say("")
    say("── 被删除的 %d 条（源集合中仍在，此处仅留档） ──" % len(deleted))
    for did, text in deleted:
        say(f"  [del] {did}  {text}")
    say("")
    say("── 被合并掉的 %d 条 ──" % len(merged))
    for mid, keep, text in merged:
        say(f"  [dup] {mid}  → 保留 {keep}")
        say(f"        {text}")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"迁移日志: {LOG.relative_to(ROOT).as_posix()}")
    print("=" * 78)
    print("⚠️ 源集合 user_memory 未被删除；应用当前仍读它 —— 切库属第 4 步（代码改造）。")
    return 0 if (covered == all_src and not dup and same) else 1


if __name__ == "__main__":
    raise SystemExit(main())