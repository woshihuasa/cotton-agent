# -*- coding: utf-8 -*-
"""把原始轨迹导出成 ShareGPT JSONL（可被 HuggingFace `datasets` 直接加载）。

【为什么需要这个工具】原始轨迹（`config.TRAJECTORY_PATH` 录的那些）是**内部格式**，
    便于调试与再加工，但不适合直接喂下游。这个脚本把它转成社区通行格式：

    · **ShareGPT**：`conversations = [{from, value}, ...]`，`from` ∈ human/gpt/tool
    · **推理统一为 `<think>…</think>`**，无推理时补空块（保证列结构一致）
    · **工具调用包 XML**：`<tool_call>` / `<tool_response>`
    · **`tool_stats` 恒含全部工具名**（值为 0 也写）

【为什么导出要和录制分开】
    ★ 导出是**纯函数派生**，格式改了**只重导、不重跑** ——
    重跑问答要花 API 钱，而换格式不该花这个钱。

【用法】

    # 1) 开录制（写进 .env，或在命令里临设）
    set TRAJECTORY_PATH=tests/eval_outputs/trajectory.jsonl

    # 2) 正常用一阵（跑评测 / 手动问答）

    # 3) 导出
    python tools/export_trajectory.py
    python tools/export_trajectory.py --src a.jsonl --dst out.jsonl --quiet

    # 4) 验证能被 datasets 加载（有安装时）
    python -c "from datasets import load_dataset; d=load_dataset('json', data_files='out.jsonl'); print(d)"

【退出码】0 = 成功（含"0 条"）；2 = 源文件不存在。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import trajectory                                   # noqa: E402


def _default_src() -> str:
    return trajectory.trajectory_path()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="原始轨迹 → ShareGPT JSONL（HF datasets 可直接加载）")
    ap.add_argument("--src", default="",
                    help="原始轨迹 JSONL（默认取 config.TRAJECTORY_PATH）")
    ap.add_argument("--dst", default="",
                    help="输出路径（默认同目录下 <src>_sharegpt.jsonl）")
    ap.add_argument("--no-empty-think", action="store_true",
                    help="不给无推理的回答补空 <think> 块（默认补，保证列一致）")
    ap.add_argument("--quiet", action="store_true", help="只输出错误")
    args = ap.parse_args()

    src = (args.src or _default_src()).strip()
    if not src:
        print("✗ 未指定 --src，且 config.TRAJECTORY_PATH 为空。", file=sys.stderr)
        print("  先开启录制：set TRAJECTORY_PATH=tests/eval_outputs/trajectory.jsonl",
              file=sys.stderr)
        return 2
    src_path = Path(src)
    if not src_path.is_file():
        print(f"✗ 源文件不存在: {src_path}", file=sys.stderr)
        return 2

    dst = args.dst or str(src_path.with_name(src_path.stem + "_sharegpt.jsonl"))
    stats = trajectory.export_file(
        src_path, dst, include_empty_think=not args.no_empty_think)

    if not args.quiet:
        print(f"源文件 : {src_path}  ({src_path.stat().st_size:,} B)")
        print(f"读出   : {stats['read']} 条原始轨迹")
        print(f"写出   : {stats['written']} 条 ShareGPT")
        if stats["skipped"]:
            print(f"跳过   : {stats['skipped']} 条（answer 为空 = 那一轮没有产出）")
        print(f"输出   : {dst}")

        # 抽样展示一条，便于肉眼确认格式
        try:
            first = json.loads(Path(dst).read_text(encoding="utf-8").splitlines()[0])
            print()
            print("首条记录的列（HF datasets 的列就是这些）：")
            for k in first:
                v = first[k]
                desc = (f"{len(v)} 轮对话" if k == "conversations"
                        else f"{len(v)} 个字段" if isinstance(v, dict)
                        else str(v)[:48])
                print(f"  {k:12s} {desc}")
        except (OSError, IndexError, json.JSONDecodeError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())