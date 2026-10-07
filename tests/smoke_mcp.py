# -*- coding: utf-8 -*-
"""MCP 冒烟测试 —— 分两层，越靠后越接近真实使用。

  [1] 改形层（纯函数，零 IO）：`to_mcp_tool` / `with_mcp_overrides` / `to_content_blocks`
  [2] 集成层（真起 stdio 子进程）：initialize → tools/list → tools/call

第 2 层特意验证**工具契约的单一来源**：MCP `tools/list` 返回的 inputSchema 必须与
`core/tools/schemas.py` 逐字段一致 —— 这正是"不重新声明工具"要守住的性质。

用法:
    python tests/smoke_mcp.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import anyio                                          # noqa: E402
from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402

from config import AppConfig                          # noqa: E402
from core.tools import TOOL_HANDLERS, TOOL_SCHEMAS, ToolResult  # noqa: E402
from core.tools.mcp_adapter import (                  # noqa: E402
    to_content_blocks,
    to_mcp_tool,
    with_mcp_overrides,
)

PASS = 0
FAIL = 0


def chk(cond: bool, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    [OK ] {label}")
    else:
        FAIL += 1
        print(f"    [FAIL] {label} {extra}")


def png_count() -> int:
    d = Path(AppConfig.CHART_DIR)
    return len(list(d.glob("*.png"))) if d.exists() else 0


# ══════════════════════════ [1] 改形层（纯函数） ══════════════════════════
def part1() -> None:
    print("=" * 74)
    print("[1] 改形层（纯函数，零 IO）")
    print("=" * 74)

    converted = [to_mcp_tool(s) for s in TOOL_SCHEMAS]
    print(f"  契约 {len(TOOL_SCHEMAS)} 条 → MCP Tool {len(converted)} 条")

    chk(all(set(c) == {"name", "description", "inputSchema"} for c in converted),
        "字段恰为 name / description / inputSchema（无多余包装）")

    names = [c["name"] for c in converted]
    chk(sorted(names) == sorted(TOOL_HANDLERS),
        "MCP 工具名与实现注册表一一对应", f"{names}")

    # 逐字段核对：inputSchema 必须与源契约**同一对象内容**
    same = all(c["inputSchema"] == s["function"]["parameters"]
               and c["description"] == s["function"]["description"]
               for c, s in zip(converted, TOOL_SCHEMAS))
    chk(same, "inputSchema / description 与源契约逐字段一致（单一来源）")

    ov = with_mcp_overrides("plot_trend", {"metric": "yield"})
    chk(ov.get("_materialize") is False, "plot_trend 在 MCP 侧被关掉落盘")
    ov2 = with_mcp_overrides("query_cotton_stats", {"metric": "yield"})
    chk("_materialize" not in ov2, "其他工具不被注入内部开关")
    src = {"metric": "yield"}
    with_mcp_overrides("plot_trend", src)
    chk("_materialize" not in src, "不改动调用方传入的 dict")

    blocks = to_content_blocks(ToolResult(text="hi", images=[(b"\x89PNG\r\n\x1a\nxyz", "image/png")]))
    chk(len(blocks) == 2 and blocks[0].type == "text" and blocks[1].type == "image",
        "ToolResult → [文本块, 图片块]")
    try:
        decoded = base64.b64decode(blocks[1].data)
        ok_b64 = decoded.startswith(b"\x89PNG")
    except Exception:
        ok_b64 = False
    chk(ok_b64, "图片块为合法 base64 且还原后是 PNG")
    chk(to_content_blocks(ToolResult(text="x"))[0].text == "x", "无图时只回文本块")


# ══════════════════════════ [2] 集成层（真 stdio） ══════════════════════════
async def part2() -> None:
    print()
    print("=" * 74)
    print("[2] 集成层（真实 stdio 子进程：initialize → tools/list → tools/call）")
    print("=" * 74)

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py")],
        env=None,                       # 继承当前环境（含 .env 之外的变量）
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"  已连接: {init.server_info.name} v{init.server_info.version}")
            chk(init.server_info.name == "cotton-agent", "服务标识正确")
            chk(init.capabilities.tools is not None, "声明了 tools 能力")

            listed = await session.list_tools()
            tnames = [t.name for t in listed.tools]
            print(f"  tools/list → {len(tnames)} 个: {', '.join(tnames)}")
            chk(len(tnames) == len(TOOL_SCHEMAS), "工具数与契约一致")

            # 单一来源的关键断言：MCP 侧 schema 必须等于本地契约
            # 注意：Tool 是 pydantic 模型，属性为蛇形 input_schema；
            # inputSchema 只是**线上的别名**（序列化时才出现）
            by_name = {t.name: t for t in listed.tools}
            match = all(
                by_name[s["function"]["name"]].input_schema == s["function"]["parameters"]
                for s in TOOL_SCHEMAS
            )
            chk(match, "★ MCP 暴露的 inputSchema 与本地契约完全一致（无漂移）")

            # ── 调用本地数据工具 ──
            r = await session.call_tool("query_cotton_stats",
                                        {"region": "阿克苏地区", "year": 2022,
                                         "metric": "yield", "stat": "value"})
            txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            print(f"  call query_cotton_stats → {txt[:96]}")
            chk((not r.is_error) and "1027056" in txt, "统计数据工具返回正确数值")

            # ── 调用绘图工具：必须内联出图，且**不落盘** ──
            before = png_count()
            r2 = await session.call_tool("plot_trend",
                                         {"metric": "yield", "regions": ["阿克苏地区"],
                                          "start": "2018", "end": "2022"})
            after = png_count()
            imgs = [b for b in r2.content if getattr(b, "type", "") == "image"]
            body = json.loads("".join(b.text for b in r2.content
                                      if getattr(b, "type", "") == "text"))
            print(f"  call plot_trend → 图片块 {len(imgs)} 个，"
                  f"base64 长度 {len(imgs[0].data) if imgs else 0}，"
                  f"chart_path 字段 {'有' if 'chart_path' in body else '无'}")
            chk(len(imgs) == 1, "图片作为 image content block 内联返回")
            chk(imgs and base64.b64decode(imgs[0].data).startswith(b"\x89PNG"),
                "内联图片是合法 PNG")
            chk("chart_path" not in body, "★ 不再回传本地路径（MCP 宿主读不到）")
            chk(before == after, f"★ MCP 调用未在磁盘留文件（{before} → {after}）")

            # ── 未知工具：应回错误结果而非断连 ──
            r3 = await session.call_tool("no_such_tool", {})
            msg = "".join(b.text for b in r3.content if getattr(b, "type", "") == "text")
            chk(r3.is_error and "未知工具" in msg, "未知工具返回 is_error 且说明原因")


# ═══════════════════ [3] 协议通道保护（stdout 不得被污染） ═══════════════════
def part3() -> None:
    print()
    print("=" * 74)
    print("[3] 协议通道保护（stdio 的 stdout 承载 JSON-RPC，不能被 print 污染）")
    print("=" * 74)

    # 3a. 静态：工具层不得出现 print(
    offenders: list[str] = []
    for py in sorted((ROOT / "core" / "tools").rglob("*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "print(" in code and "builtins.print" not in code:
                offenders.append(f"{py.name}:{i}")
    chk(not offenders, "工具层无 print() 调用", f"违规: {offenders}")

    # 3b. 行为：入口把 print 改道到 stderr（子进程实测，不污染本进程）
    probe = ("import sys; sys.path.insert(0, r'%s'); "
             "import mcp_server; print('__STRAY__')" % ROOT)
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    diag = (f"rc={r.returncode} stdout={(r.stdout or '')[:80]!r} "
            f"stderr={(r.stderr or '')[:300]!r}")
    chk(r.returncode == 0, "子进程能正常加载入口模块", diag)
    chk("__STRAY__" not in (r.stdout or ""),
        "加载入口后 print 不再写入 stdout（协议通道安全）", diag)
    chk("__STRAY__" in (r.stderr or ""),
        "该 print 确实被改道到了 stderr", diag)


def main() -> int:
    part1()
    part3()
    anyio.run(part2)
    print()
    print("=" * 74)
    print(f"MCP 冒烟结果: {PASS}/{PASS + FAIL} 通过")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
