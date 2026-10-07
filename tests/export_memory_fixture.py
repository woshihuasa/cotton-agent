# -*- coding: utf-8 -*-
"""导出用户记忆（L4 实体记忆 + L3 摘要）为固定 fixture。

【用途】
  · S1 重构（拆分 UserMemoryStore）的回归网——沙箱加载 fixture 后，行为应与生产一致
  · 让生成层评测在隔离沙箱中得到与旧基线相同的记忆上下文（保持指标可比）

【产物】
  <out>/db/                       源记忆库的**目录快照**（权威沙箱源：直接复制即可使用）
  <out>/user_memory.jsonl         每行 {id, document, metadata, embedding_b64, dim}
  <out>/session_summaries.jsonl   同上
  <out>/manifest.json             导出时间 / 源路径 / 条数 / 源库校验和

  · db/ 快照是**沙箱的首选来源**——复制后 ChromaDB 直接打开，向量与索引逐位一致。
  · JSONL 是**可读归档**，供人工审计与 S6 记忆治理（去重 / 剔除污染条目）时查阅。
    ⚠️ 注意：从 JSONL **重建**新库需要 chromadb 具备"新建数据库"能力；
       在受限 shell（DSH 沙箱）下该操作会被拒（SQLite code 14），普通环境正常。

⚠️ 产物含**真实用户画像**，输出目录必须已被 .gitignore 排除（默认 tests/eval_fixtures/）。
   严禁把该目录提交到任何仓库。

【用法】
    python tests/export_memory_fixture.py                    # 默认导出
    python tests/export_memory_fixture.py --out <dir>        # 指定输出目录
    python tests/export_memory_fixture.py --src <dir>        # 指定源记忆库目录
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import AppConfig  # noqa: E402

COLLECTIONS = ("user_memory", "session_summaries")


def _sha256_dir(path: Path) -> str:
    """对源库目录内所有文件做汇总校验和，便于确认 fixture 对应哪次快照。"""
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(f.relative_to(path).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(f.read_bytes())
    return h.hexdigest()


def _encode(vec) -> str:
    """向量以 float32 原始字节 base64 存储，保证 round-trip 精度无损。"""
    arr = np.asarray(vec, dtype=np.float32)
    return base64.b64encode(arr.tobytes()).decode("ascii")


def export_collection(client, name: str, out_path: Path) -> dict:
    col = client.get_or_create_collection(name)
    data = col.get(include=["documents", "metadatas", "embeddings"])

    ids = data.get("ids") or []
    docs = data.get("documents") or []
    metas = data.get("metadatas") or []
    embs = data.get("embeddings")

    n = len(ids)
    dim = 0
    with out_path.open("w", encoding="utf-8") as f:
        for i in range(n):
            emb = None if embs is None else embs[i]
            rec = {
                "id": ids[i],
                "document": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "embedding_b64": None if emb is None else _encode(emb),
                "dim": 0 if emb is None else int(np.asarray(emb).size),
            }
            if rec["dim"]:
                dim = rec["dim"]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {"name": name, "count": n, "dim": dim, "file": out_path.name}


def main() -> int:
    ap = argparse.ArgumentParser(description="导出用户记忆为固定 fixture")
    ap.add_argument("--src", default=AppConfig.USER_MEMORY_DB_PATH,
                    help="源记忆库目录（默认取 AppConfig.USER_MEMORY_DB_PATH）")
    ap.add_argument("--out", default=str(ROOT / "tests" / "eval_fixtures" / "memory"),
                    help="输出目录（默认 tests/eval_fixtures/memory）")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    if not src.exists():
        print(f"[错误] 源记忆库不存在: {src}")
        return 2

    out.mkdir(parents=True, exist_ok=True)

    # 先做目录快照——必须在打开 ChromaDB 客户端之前（避免 Windows 文件锁）
    db_snapshot = out / "db"
    if db_snapshot.exists():
        shutil.rmtree(db_snapshot)
    shutil.copytree(src, db_snapshot)
    print(f"  目录快照: {db_snapshot.name}/ （{sum(1 for _ in db_snapshot.rglob('*') if _.is_file())} 个文件）")

    client = chromadb.PersistentClient(path=str(src))

    results = []
    for name in COLLECTIONS:
        info = export_collection(client, name, out / f"{name}.jsonl")
        results.append(info)
        print(f"  导出 {name}: {info['count']} 条" +
              (f"（{info['dim']} 维）" if info["dim"] else "（无向量）"))

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(src),
        "source_sha256": _sha256_dir(src),
        "db_snapshot": "db",
        "collections": results,
        "warning": "含真实用户画像，严禁入库（须由 .gitignore 排除）",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n已写入: {out}")
    print(f"源库校验和: {manifest['source_sha256'][:16]}...")
    total = sum(r["count"] for r in results)
    print(f"合计 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
