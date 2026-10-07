# -*- coding: utf-8 -*-
"""E4 记忆评测集 —— 三个维度的量化评估。

【三个维度】
  ① 冲突消解准确率 —— 对 (旧记忆, 新事实) 走**真实写入路径**（`RAGEngine.resolve_memory_fact`），
     核对 ADD / UPDATE / DELETE / NOOP 四分类。指标：准确率 + 分类混淆。
  ② 去重率         —— 按场景顺序写入若干条表述同一事实的话，看最终条目数是否收敛。
     指标：场景达标数 + 条目膨胀率。**派生事实**场景单列为已知边界。
  ③ 记忆检索 Hit@K —— 预置记忆库后用口语化 query 检索。指标：Hit@1 / Hit@3。

【与其它测试的分工】
  · `smoke_memory.py`      —— "记忆还活着吗"（确定性、零 LLM）→ 回归守卫
  · `check_l4_conflict.py` —— 只测 prompt 与解析（不碰存储、不起引擎）→ 快速定标
  · **本脚本**             —— 覆盖三个维度、走真实引擎路径 → 正式评测

【隔离】全部在沙箱记忆库上运行，绝不触碰生产库。
        优先直接新建；受限环境下回退为"复制 fixture 后清空"。

【成本】约 60 次 LLM 调用 + 约 70 次 Embedding；`--only` 可只跑单节。

【用法】
    python tests/eval_memory.py                    # 全部
    python tests/eval_memory.py --only conflict    # 只跑冲突消解
    python tests/eval_memory.py --repeat 3         # 冲突消解跑 3 轮看稳定性
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core.rag_engine import RAGEngine                       # noqa: E402
from core.user_memory import UserMemoryStore                # noqa: E402

DATA = ROOT / "tests" / "eval_data" / "memory_set.jsonl"
FIXTURE_DB = ROOT / "tests" / "eval_fixtures" / "memory" / "db"
SANDBOX = ROOT / "tests" / "eval_outputs" / "memory_eval_sandbox"
COLL_ATTRS = ("_memory_collection", "_summary_collection")

TARGET_CONFLICT = 90.0
TARGET_RETRIEVAL = 80.0


def load_set():
    seed, cases = [], []
    for line in DATA.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if o.get("type") == "seed":
            seed = o.get("facts", [])
        else:
            cases.append(o)
    return seed, cases


def wipe(store: UserMemoryStore) -> None:
    """清空沙箱记忆库（走私有集合句柄；与 smoke_memory.py 同一做法）。"""
    for attr in COLL_ATTRS:
        col = getattr(store, attr)
        ids = col.get(include=[]).get("ids") or []
        if ids:
            col.delete(ids=ids)


def _can_create_chroma() -> bool:
    """探测本环境能否**新建** ChromaDB 持久库（在独立子目录里试，不污染目标目录）。

    受限环境（如 DSH 沙箱）会抛 SQLite code 14；且失败时已创建的 sqlite 文件
    会被占用而无法删除，因此**必须先探测、再决定**，不能"先建后删重来"。
    """
    probe = SANDBOX / "_probe"
    try:
        UserMemoryStore(db_path=str(probe))
        return True
    except Exception:
        return False


def prepare_sandbox_dir() -> str:
    """备好沙箱记忆库**目录**，返回其路径（供 RAGEngine(user_data_dir=...) 使用）。

    必须在构造 RAGEngine **之前**调用——引擎自身会去新建记忆库目录。
    支持新建则交给引擎建空库；否则复制 fixture 顶上（内容随后由调用方清空）。
    """
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True)
    db = SANDBOX / "chroma_user_memory"

    if _can_create_chroma():
        print("[Sandbox] 环境支持新建 → 交由引擎创建空记忆库")
        return str(db)

    print("[Sandbox] 环境不支持新建 ChromaDB 库 → 复制 fixture 顶上（稍后清空）")
    if not FIXTURE_DB.exists():
        raise SystemExit(
            f"[致命] 既不能新建记忆库，也找不到 fixture: {FIXTURE_DB}\n"
            f"        请先运行: python tests/export_memory_fixture.py"
        )
    shutil.copytree(FIXTURE_DB, db)
    return str(db)


def eval_conflict(engine, cases, repeat):
    print("\n" + "=" * 74)
    print(f"① 冲突消解准确率（{len(cases)} 条 × {repeat} 轮，走真实写入路径）")
    print("=" * 74)
    tally = collections.defaultdict(list)
    conf = collections.Counter()
    total_ok = total = 0
    for rnd in range(repeat):
        if repeat > 1:
            print(f"\n  ── 第 {rnd + 1}/{repeat} 轮 ──")
        for c in cases:
            wipe(engine.memory)
            engine.memory.add_l4_memory(c["old"])
            got = engine.resolve_memory_fact(c["new"])
            ok = got in c["expected"]
            tally[c["id"]].append(ok)
            conf[(got, tuple(c["expected"]))] += 1
            total += 1
            total_ok += ok
            if repeat == 1 or not ok:
                mark = "OK  " if ok else "FAIL"
                print(f"  [{mark}] #{c['id']:<3} {got:<7} 期望 {'/'.join(c['expected']):<14}"
                      f" {c['note'][:30]}")
    acc = total_ok / total * 100 if total else 0.0
    print(f"\n  分类混淆（实际 → 期望）:")
    for (got, exp), n in sorted(conf.items(), key=lambda kv: -kv[1])[:8]:
        flag = "" if got in exp else "  ← 误判"
        print(f"    {got:<7} → {'/'.join(exp):<14} {n:>3} 次{flag}")
    unstable = {k: v for k, v in tally.items() if not all(v)}
    if repeat > 1 and unstable:
        print(f"  不稳定/失败样本: {sorted(unstable)}")
    print(f"\n冲突消解准确率: {total_ok}/{total} = {acc:.1f}%  （目标 ≥ {TARGET_CONFLICT:.0f}%）")
    return total_ok, total, acc


def eval_dedup(engine, cases):
    print("\n" + "=" * 74)
    print(f"② 去重率（{len(cases)} 个场景，顺序写入后核对最终条目数）")
    print("=" * 74)
    ok_sc, n_sc = 0, 0
    exp_sum = act_sum = 0
    boundaries = []
    for c in cases:
        wipe(engine.memory)
        step_ok = 0
        for s in c["steps"]:
            got = engine.resolve_memory_fact(s["fact"])
            step_ok += got in {s["expect"]}
        actual = engine.memory.counts()["user_memory"]
        exp = c["expected_final"]
        # 内容断言：仅看条数会被"销毁一条 + 新增一条"骗过（#104 曾如此）
        stored = set(engine.memory._memory_collection.get(include=["documents"])["documents"])
        need = c.get("expected_present", [])
        missing = [f for f in need if f not in stored]
        good = (actual == exp) and not missing
        tag = "已知边界" if c.get("boundary") else ""
        print(f"\n  [{('OK  ' if good else 'FAIL')}] #{c['id']} {c['scenario']} {tag}")
        print(f"        步骤动作 {step_ok}/{len(c['steps'])} 正确 ｜ 最终 {actual} 条（期望 {exp}）")
        if need:
            print(f"        必须仍在库中的事实: {'全部在场' if not missing else '缺失 ' + str(missing)}")
        for s in c["steps"]:
            print(f"          · {s['fact'][:44]:<46} 期望 {s['expect']}")
        if c.get("boundary"):
            boundaries.append((c["id"], actual, exp))
        else:
            n_sc += 1
            ok_sc += good
            exp_sum += exp
            act_sum += actual
    rate = ok_sc / n_sc * 100 if n_sc else 0.0
    infl = act_sum / exp_sum if exp_sum else 0.0
    print(f"\n去重率: {ok_sc}/{n_sc} = {rate:.1f}%  （非边界场景条目膨胀率 {infl:.2f}）")
    for i, a, e in boundaries:
        print(f"  已知边界 #{i}: 最终 {a} 条 / 期望 {e} 条 —— 冲突检查为 1 对 1，无法同时比较派生来源")
    return ok_sc, n_sc, rate


def eval_retrieval(engine, seed, cases, k=3):
    print("\n" + "=" * 74)
    print(f"③ 记忆检索 Hit@K（种子 {len(seed)} 条，{len(cases)} 条 query）")
    print("=" * 74)
    wipe(engine.memory)
    for f in seed:
        engine.memory.add_l4_memory(f)
    print(f"  已预置 {engine.memory.counts()['user_memory']} 条记忆\n")
    h1 = hk = 0
    for c in cases:
        got = engine.memory.retrieve_l4_memory(c["query"], k=k)
        exp = set(c["expected"])
        top1 = bool(got) and got[0] in exp
        hit = any(g in exp for g in got)
        h1 += top1
        hk += hit
        mark = "OK  " if hit else "FAIL"
        first = got[0][:34] if got else "(空)"
        print(f"  [{mark}] #{c['id']} {c['query'][:18]:<20} top1={'√' if top1 else '×'}  {first}")
    n = len(cases)
    print(f"\n记忆检索 Hit@1: {h1}/{n} = {h1 / n * 100:.1f}%")
    print(f"记忆检索 Hit@{k}: {hk}/{n} = {hk / n * 100:.1f}%  （目标 ≥ {TARGET_RETRIEVAL:.0f}%）")
    return hk, n, hk / n * 100


def main() -> int:
    ap = argparse.ArgumentParser(description="E4 记忆评测集")
    ap.add_argument("--only", default="all",
                    choices=["all", "conflict", "dedup", "retrieval"])
    ap.add_argument("--repeat", type=int, default=1, help="冲突消解重复轮数")
    args = ap.parse_args()

    seed, cases = load_set()
    by_type = collections.defaultdict(list)
    for c in cases:
        by_type[c["type"]].append(c)
    print(f"记忆评测集: {len(cases)} 条 "
          f"（冲突 {len(by_type['conflict'])} / 去重 {len(by_type['dedup'])} / "
          f"检索 {len(by_type['retrieval'])}）+ 种子 {len(seed)} 条")

    prepare_sandbox_dir()
    engine = RAGEngine(user_data_dir=SANDBOX)     # 沙箱作用域，绝不碰生产
    wipe(engine.memory)                           # 走回退路径时清空 fixture 内容
    print(f"[Sandbox] 起始状态: {engine.memory.counts()}")

    if args.only in ("all", "conflict"):
        c_ok, c_n, _ = eval_conflict(engine, by_type["conflict"], args.repeat)
    if args.only in ("all", "dedup"):
        d_ok, d_n, _ = eval_dedup(engine, by_type["dedup"])
    if args.only in ("all", "retrieval"):
        r_ok, r_n, _ = eval_retrieval(engine, seed, by_type["retrieval"])

    print("\n" + "=" * 74)
    print("E4 汇总")
    print("=" * 74)
    all_ok = True
    if args.only in ("all", "conflict"):
        ok = c_ok / c_n * 100 >= TARGET_CONFLICT
        all_ok &= ok
        print(f"  冲突消解准确率 : {c_ok}/{c_n} = {c_ok / c_n * 100:.1f}%  {'✅' if ok else '❌'}")
    if args.only in ("all", "dedup"):
        print(f"  去重率         : {d_ok}/{d_n} = {d_ok / d_n * 100:.1f}%")
    if args.only in ("all", "retrieval"):
        ok = r_ok / r_n * 100 >= TARGET_RETRIEVAL
        all_ok &= ok
        print(f"  记忆检索 Hit@3 : {r_ok}/{r_n} = {r_ok / r_n * 100:.1f}%  {'✅' if ok else '❌'}")
    print("=" * 74)

    prod = UserMemoryStore()
    print(f"隔离性核对 —— 生产记忆库: {prod.counts()}")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
