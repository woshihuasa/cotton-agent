# -*- coding: utf-8 -*-
"""L4 一次性维护：① 清掉评测运行残留  ② 回填 `asserted_at`（近似）。

【两件事，两个判据，都**只看结构化字段、不看内容语义】**
        记忆内容本身没有现实情境（是功能测试时的即兴输入），因此任何基于
        "这句话像不像真事"的判断都没有意义。可用且可靠的只有**元数据**：
        时间戳、来源、类别、所在库。

【① 清评测运行残留】
        判据：`updated_at` 落在**文档记录的评测窗口**内
        （`2026-09-29T15:28Z ~ 15:40Z`，见 `_会话上下文存档.md` / ROADMAP S5·S6）。
        实测该窗口内命中 50 条，时间分布 15:29–15:38，与窗口吻合。
        → 这批是**评测脚本运行时写入生产库**的产物（历史上"评测污染"的一部分）。

【② 回填 `asserted_at`（方案 b：用 updated_at 近似）】
        `asserted_at` 是"用户说出这条事实的时间"，冲突消解要用它。
        但它**只能从现在开始精确记录** —— 历史条目的真实断言时间已不可考。
        故用 `updated_at` 近似回填，并把 `asserted_at_source` 标为
        `backfill_approx`，以免日后被当作精确值使用。
        （集中落在归并/导入日的条目，其近似值尤其不可信；字段本身带上来源即可自证。）

【安全网】与分家同一纪律：**先备份、再动手**；两库与源集合同时维护，
        保持 `源 == 画像 ∪ 项目` 这个不变量。

【用法】python tests/backfill_and_clean_l4.py
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

import chromadb                                            # noqa: E402
from core.memory_scope import PROFILE_COLLECTION, PROJECT_COLLECTION   # noqa: E402

DB_PATH = ROOT / "chroma_user_memory"
LEGACY = "user_memory"
OUT_DIR = ROOT / "tests" / "eval_outputs"
LOG = OUT_DIR / "l4_maintenance_log.txt"

# 文档记录的评测窗口（UTC）
EVAL_WINDOW = ("2026-09-29T15:28", "2026-09-29T15:40")


def in_eval_window(meta: dict) -> bool:
    """该条目是否由评测运行写入（**只看时间戳，不看内容**）。"""
    ts = str((meta or {}).get("updated_at") or (meta or {}).get("timestamp") or "")
    if not ts:
        return False
    return EVAL_WINDOW[0] <= ts[:16] <= EVAL_WINDOW[1]


def main() -> int:
    lines: list[str] = []

    def say(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    say("=" * 78)
    say("L4 一次性维护：清评测残留 + 回填 asserted_at")
    say("=" * 78)

    # ---- 备份 ----
    baks = sorted(OUT_DIR.glob("l4_maint_backup_*"))
    if baks:
        bak = baks[0]
        say(f"[0] 复用已有备份: {bak.name}")
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = OUT_DIR / f"l4_maint_backup_{stamp}"
        shutil.copytree(DB_PATH, bak)
        say(f"[0] 新建备份: {bak.name}")
    n_bak = chromadb.PersistentClient(path=str(bak)).get_collection(LEGACY).count()
    say(f"    备份可打开，源集合 = {n_bak} 条")

    client = chromadb.PersistentClient(path=str(DB_PATH))
    cols = {n: client.get_collection(n)
            for n in (PROFILE_COLLECTION, PROJECT_COLLECTION, LEGACY)}

    # ---- ① 清评测残留 ----
    say("")
    say("[1] 清评测运行残留（判据：updated_at ∈ 文档记录的评测窗口）")
    removed_total = 0
    removed_per: dict[str, list[str]] = {}
    for name, col in cols.items():
        got = col.get(include=["metadatas", "documents"])
        hit = [(i, d) for i, m, d in zip(got["ids"], got["metadatas"], got["documents"])
               if in_eval_window(m)]
        if hit:
            col.delete(ids=[i for i, _ in hit])
        removed_total += len(hit)
        removed_per[name] = [i for i, _ in hit]
        say(f"    {name:<15} 删除 {len(hit)} 条")
        for i, d in hit[:3]:
            say(f"        · {d[:56]}")
        if len(hit) > 3:
            say(f"        …… 其余 {len(hit) - 3} 条见日志")
        for i, d in hit:
            lines.append(f"    [del] {name} {i} {d}")

    # ---- ② 回填 asserted_at ----
    say("")
    say("[2] 回填 asserted_at（来源标 backfill_approx）")
    for name, col in cols.items():
        got = col.get(include=["metadatas"])
        updates: list[str] = []
        metas: list[dict] = []
        for i, m in zip(got["ids"], got["metadatas"]):
            m = dict(m or {})
            if m.get("asserted_at"):
                continue
            approx = str(m.get("updated_at") or m.get("timestamp") or "")
            m["asserted_at"] = approx
            m["asserted_at_source"] = "backfill_approx"
            updates.append(i)
            metas.append(m)
        if updates:
            col.update(ids=updates, metadatas=metas)
        say(f"    {name:<15} 回填 {len(updates)} 条")

    # ---- 校验 ----
    say("")
    say("[3] 校验")
    prof, proj = cols[PROFILE_COLLECTION], cols[PROJECT_COLLECTION]
    src = cols[LEGACY]
    say(f"    {PROFILE_COLLECTION}={prof.count()} · {PROJECT_COLLECTION}={proj.count()} "
        f"· {LEGACY}={src.count()}")
    say(f"    不变量 源 == 画像 ∪ 项目 → "
        f"{'成立 ✓' if src.count() == prof.count() + proj.count() else '**不成立 ✗**'}")

    missing = 0
    for name, col in cols.items():
        got = col.get(include=["metadatas"])
        missing += sum(1 for m in got["metadatas"] if not (m or {}).get("asserted_at"))
    say(f"    仍缺 asserted_at 的条目: {missing} → "
        f"{'全部已覆盖 ✓' if missing == 0 else '**仍有缺失**（无任何时间字段可回填）'}")

    sample = prof.get(limit=2, include=["metadatas", "documents"])
    for m, d in zip(sample["metadatas"], sample["documents"]):
        say(f"    样例: asserted_at={str(m.get('asserted_at'))[:19]} "
            f"src={m.get('asserted_at_source')}  「{d[:34]}」")

    say("")
    n_prof = len(removed_per.get(PROFILE_COLLECTION, []))
    n_proj = len(removed_per.get(PROJECT_COLLECTION, []))
    n_src = len(removed_per.get(LEGACY, []))
    say(f"共删除 **{n_prof + n_proj} 条唯一残留**（画像 {n_prof} + 项目 {n_proj}）")
    say(f"`{LEGACY}` 是两库并集，同批条目在其中被重复计数（{n_src}）→ **不累加它**，"
        f"否则会把 60 条说成 120 条")
    say(f"备份: {bak.name}")
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"维护日志: {LOG.relative_to(ROOT).as_posix()}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())