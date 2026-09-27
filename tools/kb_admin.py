# -*- coding: utf-8 -*-
"""
知识库管理工具（KB Admin）
============================

【用途】
  对 RAG 知识库做**增量**维护 —— 增 / 删 / 改都不需要重建整个向量库。

【核心机制】
  以「文件内容 SHA256」作为变更指纹（写入每个 chunk 的 metadata），
  并以 metadata["source"] 定位片段，因此：
    · 新增：只切分并向量化新文件
    · 修改：只对变化的文件「删旧片段 + 建新片段」
    · 删除：按 source 精确移除该文件的全部片段

  注意：**修改**之所以可行，依赖两点 ——
    ① 每个 chunk 都记录了来源文件（source，规范化绝对路径）
    ② 变更判定用「内容 hash」而非文件修改时间（mtime 不可靠）

【命令】
  status              查看 data/ 与向量库的差异（**只检测，不修改**）
  sync                按差异执行增量同步（改 1 篇只重嵌 1 篇）
  add <路径>...       把文件（或整个目录）加入知识库
  remove <名称>...    从向量库移除（默认保留源文件；--purge 同时删除源文件）
  list                列出已索引的文档及各自的片段数
  rebuild             全量重建（换嵌入模型或换切分参数时必须使用）

【示例】
  .venv\\Scripts\\python.exe tools\\kb_admin.py status
  .venv\\Scripts\\python.exe tools\\kb_admin.py sync --yes
  .venv\\Scripts\\python.exe tools\\kb_admin.py add "D:\\新文档\\2026年棉花植保意见.md"
  .venv\\Scripts\\python.exe tools\\kb_admin.py remove 2024年棉花重大病虫害防控技术方案.md
  .venv\\Scripts\\python.exe tools\\kb_admin.py list
  .venv\\Scripts\\python.exe tools\\kb_admin.py rebuild --yes

【成本】
  只有 sync / add / rebuild 会调用 Embedding API，且**仅针对新增或变化的文件**。
"""
import io
import os
import sys
import shutil
import argparse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import AppConfig                        # noqa: E402
from core.knowledge_base import KnowledgeBase      # noqa: E402

SUPPORTED = (".md", ".txt", ".pdf")


def fmt(path: str) -> str:
    """只显示文件名（路径太长时缩短，便于阅读）。"""
    p = Path(path)
    return p.name


def cmd_status(kb: KnowledgeBase, full: bool = False) -> None:
    diff = kb.diff_index()
    print("=" * 64)
    print("知识库差异检测")
    print("=" * 64)
    for key, label in (("added", "新增"), ("updated", "内容有变化"), ("removed", "已删除")):
        items = diff[key]
        print(f"\n【{label}】{len(items)} 篇")
        for p in items:
            print(f"    {p if full else fmt(p)}")
    total = sum(len(v) for v in diff.values())
    print("\n" + "-" * 64)
    if total == 0:
        print("✅ 向量库与 data/ 完全一致，无需同步。")
    else:
        print(f"⚠️ 共 {total} 篇待处理。执行 `sync` 可增量同步。")


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_sync(kb: KnowledgeBase, assume_yes: bool) -> None:
    diff = kb.diff_index()
    added, updated, removed = diff["added"], diff["updated"], diff["removed"]
    if not (added or updated or removed):
        print("✅ 无需同步：向量库与 data/ 完全一致。")
        return

    print("=" * 64)
    print("增量同步")
    print("=" * 64)
    print(f"  新增 {len(added)} 篇　内容变化 {len(updated)} 篇　已删除 {len(removed)} 篇")
    print(f"  （只处理这 {len(added) + len(updated)} 篇的向量化，不会重建整个库）")
    for label, items in (("新增", added), ("变化", updated), ("删除", removed)):
        for p in items:
            print(f"    [{label}] {fmt(p)}")

    if not confirm("\n确认执行？", assume_yes):
        print("已取消。")
        return

    result = kb.sync_index()
    print("\n" + "-" * 64)
    print(f"✅ 同步完成：新增 {result['chunks_added']} 个片段，"
          f"移除 {result['chunks_removed']} 个片段")
    print(f"   （新增 {result['added']} / 变化 {result['updated']} / 删除 {result['removed']}）")


def collect_files(target: str) -> list[Path]:
    p = Path(target)
    if p.is_file():
        return [p] if p.suffix.lower() in SUPPORTED else []
    if p.is_dir():
        out = []
        for ext in SUPPORTED:
            out.extend(sorted(p.glob(f"**/*{ext}")))
        return out
    return []


def cmd_add(kb: KnowledgeBase, targets: list[str], assume_yes: bool) -> None:
    data_dir = Path(AppConfig.DATA_DIR)

    files: list[Path] = []
    for t in targets:
        found = collect_files(t)
        if not found:
            print(f"  ⚠️ 未找到可用文档（支持 {'/'.join(SUPPORTED)}）: {t}")
        files.extend(found)

    if not files:
        print("没有可加入的文件。")
        return

    print("=" * 64)
    print(f"将以下 {len(files)} 个文件复制到知识库目录并建立索引")
    print("=" * 64)
    for f in files:
        print(f"    {f}  →  {data_dir / f.name}")

    if not confirm("\n确认执行？", assume_yes):
        print("已取消。")
        return

    copied = []
    for f in files:
        dest = data_dir / f.name
        if f.resolve() != dest.resolve():
            shutil.copy2(f, dest)
        copied.append(str(dest))
        print(f"  已复制：{f.name}")

    result = kb.sync_index()
    print("\n" + "-" * 64)
    print(f"✅ 完成：新增 {result['chunks_added']} 个片段（来自 {len(copied)} 个文件）")


def resolve_sources(kb: KnowledgeBase, names: list[str]) -> list[str]:
    """把用户给的文件名（可只给 basename）解析为索引中的 source 全路径。"""
    indexed = kb._indexed_files()          # {规范化路径: (原始 source, hash)}
    raws = [raw for raw, _h in indexed.values()]
    out: list[str] = []
    for n in names:
        hits = [s for s in raws if s == n or Path(s).name == n or s.endswith(n)]
        if not hits:
            print(f"  ⚠️ 索引中未找到匹配：{n}")
        for h in hits:
            if h not in out:
                out.append(h)
    return out


def cmd_remove(kb: KnowledgeBase, names: list[str], purge: bool, assume_yes: bool) -> None:
    sources = resolve_sources(kb, names)
    if not sources:
        print("没有可移除的文档。")
        return

    print("=" * 64)
    print(f"将从向量库移除 {len(sources)} 篇文档的全部片段")
    print("=" * 64)
    for s in sources:
        print(f"    {s}")
    if purge:
        print("\n⚠️ --purge 已启用：源文件也会被删除。")

    if not confirm("\n确认执行？", assume_yes):
        print("已取消。")
        return

    n = kb.remove_documents(sources)
    print(f"\n✅ 已移除 {n} 个片段。")

    if purge:
        for s in sources:
            try:
                Path(s).unlink()
                print(f"  已删除源文件：{fmt(s)}")
            except OSError as e:
                print(f"  删除源文件失败 {fmt(s)}：{e}")
    else:
        print("   （源文件保留；如需一并删除请加 --purge）")


def cmd_list(kb: KnowledgeBase, full: bool = False) -> None:
    try:
        got = kb._vector_store.get(include=["metadatas"])
    except Exception as e:
        print(f"读取索引失败：{e}")
        return
    metas = got.get("metadatas") or []
    cnt = Counter((md or {}).get("source", "(未知)") for md in metas)
    print("=" * 64)
    print(f"已索引文档：{len(cnt)} 篇，片段总数：{len(metas)}"
          f"　（显示：{'完整 source' if full else '仅文件名'}）")
    print("=" * 64)
    for src, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4} 个片段   {src if full else fmt(src)}")

    if full:
        print("\n提示：以上是向量库中记录的**原始 source 字符串**；")
        print("      data/ 扫描得到的是**规范化绝对路径**，两者需一致才能匹配。")
    else:
        print("\n提示：加 --full 可显示完整 source，用于诊断路径匹配问题。")


def cmd_rebuild(kb: KnowledgeBase, assume_yes: bool) -> None:
    print("⚠️ 全量重建会清空并重新嵌入 data/ 下**所有**文档。")
    print("   仅在更换嵌入模型或调整切分参数后需要执行。")
    if not confirm("确认重建？", assume_yes):
        print("已取消。")
        return
    kb.build_vector_db()
    print("\n✅ 全量重建完成。")


def main() -> None:
    ap = argparse.ArgumentParser(description="知识库增量管理工具")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_yes(p):
        """--yes 挂在子命令上（`cmd sync --yes` 比 `cmd --yes sync` 更符合直觉）。"""
        p.add_argument("--yes", action="store_true", help="跳过确认提示")

    p_status = sub.add_parser("status", help="查看差异（不修改）")
    p_status.add_argument("--full", action="store_true", help="显示完整路径")
    add_yes(p_status)

    add_yes(sub.add_parser("sync", help="按差异执行增量同步"))

    p_list = sub.add_parser("list", help="列出已索引文档")
    p_list.add_argument("--full", action="store_true",
                        help="显示 metadata 里的完整 source（诊断路径匹配问题用）")
    add_yes(p_list)

    p_add = sub.add_parser("add", help="把文件/目录加入知识库")
    p_add.add_argument("targets", nargs="+", help="文件或目录路径")
    add_yes(p_add)

    p_rm = sub.add_parser("remove", help="从向量库移除文档")
    p_rm.add_argument("names", nargs="+", help="文件名（可只给 basename）")
    p_rm.add_argument("--purge", action="store_true", help="同时删除源文件")
    add_yes(p_rm)

    add_yes(sub.add_parser("rebuild", help="全量重建（换模型/改参数时用）"))

    args = ap.parse_args()
    assume_yes = getattr(args, "yes", False)

    print("初始化知识库 ...")
    kb = KnowledgeBase()

    if args.command == "status":
        cmd_status(kb, full=args.full)
    elif args.command == "sync":
        cmd_sync(kb, assume_yes)
    elif args.command == "add":
        cmd_add(kb, args.targets, assume_yes)
    elif args.command == "remove":
        cmd_remove(kb, args.names, args.purge, assume_yes)
    elif args.command == "list":
        cmd_list(kb, full=args.full)
    elif args.command == "rebuild":
        cmd_rebuild(kb, assume_yes)


if __name__ == "__main__":
    main()
