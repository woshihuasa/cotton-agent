# -*- coding: utf-8 -*-
"""D6 第三步收尾：把源集合 `user_memory` 裁剪成两个新库的并集。

【为什么这样裁剪，而不是"再算一遍该删哪些"】
        源集合 540 条；两个新库共 509 条（387 + 122）。
        差集恰好 = 26 条待删 + 5 条被合并掉 = 31 条 —— **不需要重新跑分类**。
        做法：删除源集合里**不在两个新库中的 id**。判据单一、可复核、不会与分类规则漂移。

【安全网】删之前先断言"备份可打开且为 540 条" —— 没验证过的备份不算备份。
        备份在 `tests/eval_outputs/d6_backup_chroma_user_memory_*/`（迁移前的原始状态）。

【用法】python tests/prune_l4_source.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import chromadb                                            # noqa: E402

DB_PATH = "chroma_user_memory"
SRC = "user_memory"
PROFILE = "user_profile"
PROJECT = "project_facts"
BACKUP_ROOT = ROOT / "tests" / "eval_outputs"


def main() -> int:
    # ---- 安全网：备份必须存在且可用 ----
    baks = sorted(BACKUP_ROOT.glob("d6_backup_chroma_user_memory_*"))
    if not baks:
        print("[FAIL] 找不到备份 —— 拒绝删源")
        return 1
    bak = baks[0]
    n_bak = chromadb.PersistentClient(path=str(bak)).get_collection(SRC).count()
    print(f"[1/4] 备份: {bak.name}  可打开，条目数 = {n_bak}")
    if n_bak != 540:
        print(f"[FAIL] 备份条目数应为 540，实际 {n_bak} —— 拒绝删源")
        return 1

    client = chromadb.PersistentClient(path=str(ROOT / DB_PATH))
    src = client.get_collection(SRC)
    prof = client.get_collection(PROFILE)
    proj = client.get_collection(PROJECT)

    src_ids = set(src.get(include=[])["ids"])
    keep = set(prof.get(include=[])["ids"]) | set(proj.get(include=[])["ids"])
    drop = sorted(src_ids - keep)
    print(f"[2/4] 源 {len(src_ids)} · 画像 {prof.count()} · 项目 {proj.count()} "
          f"· 待裁剪 {len(drop)} 条")
    print(f"      校验：{len(src_ids)} − {len(drop)} = {len(src_ids) - len(drop)}，"
          f"两库并集 = {len(keep)} → "
          f"{'一致 ✓' if len(src_ids) - len(drop) == len(keep) else '**不一致 ✗**'}")

    if not drop:
        print("[3/4] 无需裁剪（源与两库已一致）")
        return 0

    src.delete(ids=drop)
    print(f"[3/4] 已删除 {len(drop)} 条")

    after = src.count()
    ok = after == len(keep)
    print(f"[4/4] 源集合现为 {after} 条 → {'与两库并集一致 ✓' if ok else '**不一致 ✗**'}")
    print()
    print("被裁剪的条目 id（全文见 d6_migration_log.txt 与 d6_to_review.txt）:")
    for i in drop:
        print(f"  - {i}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())