# -*- coding: utf-8 -*-
"""查看本地埋点 —— 命令行版，不依赖任何服务。

埋点默认关闭。打开方式（PowerShell）：

    $env:OBSERVABILITY_ENABLED = "1"
    python main.py

记录写在 `%APPDATA%\\CottonAgent\\traces\\YYYY-MM-DD.jsonl`，一行一个 span。

用法：
    python tools/traces.py list              最近 20 条调用链
    python tools/traces.py show <trace_id>   一条链路的完整时间线
    python tools/traces.py slow --top 10     最慢的若干步，按步骤名归类
    python tools/traces.py days              每天有多少条、平均耗时
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _dir() -> Path:
    from core.observability import trace_dir
    return trace_dir()


def _load(limit_files: int = 7) -> list[dict]:
    """读最近几个文件里的全部记录。坏行跳过，不因为一行脏数据整个报错。"""
    d = _dir()
    if not d.is_dir():
        return []
    rows: list[dict] = []
    for f in sorted(d.glob("*.jsonl"))[-limit_files:]:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return rows


def _tree(rows: list[dict], trace_id: str) -> None:
    mine = [r for r in rows if r.get("trace") == trace_id]
    if not mine:
        print(f"没有找到 trace {trace_id}（看看 traces 目录：{_dir()}）")
        return
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for r in mine:
        by_parent[str(r.get("parent") or "")].append(r)

    root = next((r for r in mine if not r.get("parent")), None)
    total = max((float(r.get("ms") or 0) for r in mine), default=0.0)
    if root:
        print(f"[{trace_id}] {root.get('ts', '')}  {root.get('name', '')}"
              f"  总 {float(root.get('ms') or 0):.0f} ms")
    else:
        print(f"[{trace_id}] 总 {total:.0f} ms")

    def walk(parent: str, depth: int) -> None:
        for r in sorted(by_parent.get(parent, []),
                        key=lambda x: -float(x.get("ms") or 0)):
            pad = "   " * depth
            mark = "└─" if depth else ""
            ms = float(r.get("ms") or 0)
            extra = []
            for k in ("recall", "candidates", "top_k", "model", "tool", "type",
                      "attempt", "in", "out", "run_id"):
                if r.get(k) not in (None, "", 0):
                    extra.append(f"{k}={r[k]}")
            if r.get("error"):
                extra.append(f"error={str(r['error'])[:60]}")
            if r.get("text_query"):
                extra.append(f"query={str(r['text_query'])[:30]}")
            print(f"{pad}{mark} {r.get('name', ''):28s} {ms:7.0f}ms  "
                  + "  ".join(extra))
            walk(str(r.get("span")), depth + 1)

    walk("", 0)


def cmd_list(args: argparse.Namespace) -> int:
    rows = _load()
    roots = [r for r in rows if not r.get("parent")]
    if not roots:
        print(f"没有记录。埋点默认关闭，打开方式："
              f"$env:OBSERVABILITY_ENABLED=\"1\"，目录 {_dir()}")
        return 1
    for r in roots[-args.n:][::-1]:
        print(f"{r.get('ts', ''):20s}  {float(r.get('ms') or 0):7.0f}ms  "
              f"{r.get('trace')}  {r.get('name', '')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _tree(_load(), args.trace_id)
    return 0


def cmd_slow(args: argparse.Namespace) -> int:
    rows = [r for r in _load() if r.get("kind") in ("generation", "tool",
                                                    "retrieval", "rerank")]
    if not rows:
        print("没有可统计的步骤。")
        return 1
    rows.sort(key=lambda r: -float(r.get("ms") or 0))
    print("最慢的单个步骤：")
    for r in rows[:args.top]:
        print(f"  {float(r.get('ms') or 0):8.0f}ms  {r.get('name', ''):28s}"
              f"  {r.get('ts', '')}")
    agg: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        agg[str(r.get("name"))].append(float(r.get("ms") or 0))
    print("\n按步骤名汇总（次数 / 平均 / 最长）：")
    for name, vals in sorted(agg.items(), key=lambda kv: -sum(kv[1]))[:args.top]:
        print(f"  {name:28s} {len(vals):4d} 次  平均 {sum(vals)/len(vals):7.0f}ms"
              f"  最长 {max(vals):7.0f}ms")
    return 0


def cmd_days(args: argparse.Namespace) -> int:
    rows = _load(limit_files=60)
    per: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("parent"):
            continue
        per[str(r.get("ts", ""))[:10]].append(float(r.get("ms") or 0))
    if not per:
        print("没有记录。")
        return 1
    for day in sorted(per):
        vals = per[day]
        print(f"  {day}  {len(vals):4d} 条  平均 {sum(vals)/len(vals):6.0f}ms"
              f"  最长 {max(vals):6.0f}ms")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="查看本地埋点记录")
    sub = ap.add_subparsers(dest="cmd")
    p_list = sub.add_parser("list", help="最近若干条调用链")
    p_list.add_argument("-n", type=int, default=20)
    p_list.set_defaults(func=cmd_list)
    p_show = sub.add_parser("show", help="一条链路的完整时间线")
    p_show.add_argument("trace_id")
    p_show.set_defaults(func=cmd_show)
    p_slow = sub.add_parser("slow", help="最慢的步骤")
    p_slow.add_argument("--top", type=int, default=10)
    p_slow.set_defaults(func=cmd_slow)
    p_days = sub.add_parser("days", help="按天汇总")
    p_days.set_defaults(func=cmd_days)
    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())