# -*- coding: utf-8 -*-
"""运行视图与工作流编辑守卫（P3 + P4）—— 用 FastAPI `TestClient` 打真实路由。

【定位】`docs/ORCHESTRATION.md` §6.1 在 P3/P4 阶段的补充守卫（第 7 个）。

【守什么】
        P3（只读视图）：
          · 页面能取到、含 `<svg>`、并且**零外部依赖**（不引 CDN/前端库）；
          · 工作流列表带**节点与边**（前端布局的唯一输入）；
          · 单个 YAML 坏了**不能让整个列表 500**（错误当数据返回）；
          · 运行详情含 `state / started_at / ended_at / error`（可视化要的字段）。
        P4（编辑写回）：
          · ★ **校验不过就不写盘**（环、缺参数、id 与文件名不一致 全都要拒）；
          · 合法 YAML 能保存并**立即被列表读到**；
          · ★ `overwrite=false` 时**不覆盖**已有文件；
          · ★ 路径穿越被挡住。

【零 LLM / 零 API】所有接口都不调模型。

【用法】python tests/check_workflow_web.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient                    # noqa: E402

from core import web_server as W                             # noqa: E402
from core.orchestration import loader as _loader             # noqa: E402
from core.orchestration import store as _store               # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "_orch_web_sandbox"


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


GOOD = """
id: guard_wf
version: 1
description: 守卫用最小工作流
nodes:
  - { id: a, type: data.kb_query, params: { query: q }, side_effect: none }
"""


def main() -> int:
    c = Checker()
    SANDBOX.mkdir(parents=True, exist_ok=True)
    # 把工作流目录与运行态目录都重定向到沙箱，**不碰真实的 workflows/**
    orig_wf_dir = W._workflows_dir
    orig_runs_dir = _store.runs_dir
    W._workflows_dir = lambda: SANDBOX                            # type: ignore[assignment]
    _store.runs_dir = lambda: SANDBOX                             # type: ignore[assignment]
    client = TestClient(W.app)

    try:
        # --------------------------------------------------------------
        c.section("第 1 节：P3 页面 —— 零依赖")
        # --------------------------------------------------------------
        r = client.get("/workflow")
        c.check("页面返回 200", r.status_code == 200, str(r.status_code))
        html = r.text
        c.check("★ 含手写 SVG 画布", "<svg" in html)
        # ★ 结构性检查，**不是文本子串** —— 第一版用 `"react" in html` 判断，
        # 结果被文件里那句注释「没有 npm、没有 React」误伤。
        # 这与我早先在 `get_key` 守卫上犯的错是同一类：**用文本判语义**。
        import re as _re
        ext = _re.findall(
            r'<script[^>]+src\s*=|'
            r'<link[^>]+href\s*=\s*["\']https?://|'
            r'@import\s+url\(\s*["\']?https?://', html, _re.I)
        c.check("★★ **零外部依赖**（无外链 script / 外链样式 / 远程 @import）",
                not ext, f"发现 {ext[:3]}")
        c.check("★ 也没有包管理器痕迹（package.json / node_modules 引用）",
                "node_modules" not in html)
        c.check("★ 用 fetch 取后端数据", "fetch(" in html)
        c.check("含状态配色（done/failed/blocked）",
                all(k in html for k in ("state-done", "state-failed", "state-blocked")))

        # --------------------------------------------------------------
        c.section("第 2 节：工作流列表 —— 必须带节点与边")
        # --------------------------------------------------------------
        (SANDBOX / "guard_wf.yaml").write_text(GOOD, encoding="utf-8")
        d = client.get("/api/workflows").json()
        wfs = {w["id"]: w for w in d.get("workflows", [])}
        c.check("★ 能列出工作流", "guard_wf" in wfs, str(sorted(wfs)))
        wf = wfs.get("guard_wf") or {}
        c.check("★ 返回了节点", len(wf.get("nodes") or []) == 1, str(len(wf.get("nodes") or [])))
        c.check("★ 返回了边字段（可能为空列表）", "edges" in wf)
        c.check("节点带 type / needs / side_effect（前端要用来画）",
                all(k in (wf.get("nodes") or [{}])[0] for k in ("type", "needs", "side_effect")))

        # --------------------------------------------------------------
        c.section("第 3 节：★ 单个坏 YAML 不能让整个列表 500")
        # --------------------------------------------------------------
        (SANDBOX / "broken.yaml").write_text("id: broken\nnodes: [{{{{", encoding="utf-8")
        r2 = client.get("/api/workflows")
        c.check("★ 有坏文件时列表仍返回 200", r2.status_code == 200, str(r2.status_code))
        ids = [w["id"] for w in r2.json().get("workflows", [])]
        c.check("★ 坏文件以 error 形式出现在列表里（错误当数据）",
                "broken" in ids, str(ids))
        c.check("★ 好文件仍能正常读到", "guard_wf" in ids)
        (SANDBOX / "broken.yaml").unlink()

        # --------------------------------------------------------------
        c.section("第 4 节：P3 运行详情 —— 字段够不够画图")
        # --------------------------------------------------------------
        from core.orchestration import engine as _engine
        wf_obj = _loader.load_text(GOOD)
        run = _engine.Engine(wf_obj, persist=True).run(inputs={"__kb__": _StubKB()})
        detail = client.get(f"/api/workflows/runs/{run.run_id}").json()
        c.check("★ 运行详情能取到", "error" not in detail, str(detail.get("error"))[:60])
        c.check("★ 含 state", detail.get("state") == "done", str(detail.get("state")))
        steps = detail.get("steps") or []
        c.check("★ 含每个节点的 state", all("state" in s for s in steps))
        c.check("★ 含耗时所需的时间戳",
                all("started_at" in s and "ended_at" in s for s in steps))
        c.check("★ 时间戳精度到毫秒（否则耗时永远显示 0ms）",
                any(len(s.get("started_at") or "") > 19 for s in steps),
                str([s.get("started_at") for s in steps][:1]))
        c.check("取不存在的运行 → 返回 error 字段而不是 500",
                "error" in client.get("/api/workflows/runs/no-such-run").json())

        # --------------------------------------------------------------
        c.section("第 5 节：★★ P4 保存 —— 校验不过**不写盘**")
        # --------------------------------------------------------------
        target = SANDBOX / "to_save.yaml"
        bad_cases = [
            ("环", "id: to_save\nnodes:\n"
                   "  - { id: a, type: gate.range, params: { value: x }, side_effect: none, needs: [b] }\n"
                   "  - { id: b, type: gate.range, params: { value: y }, side_effect: none, needs: [a] }\n"),
            ("缺必填参数", "id: to_save\nnodes:\n"
                       "  - { id: a, type: tool.get_weather, params: {}, side_effect: network }\n"),
            ("参数名拼错", "id: to_save\nnodes:\n"
                        "  - { id: a, type: tool.get_weather, params: { ctiy: x }, side_effect: network }\n"),
            ("未知节点类型", "id: to_save\nnodes:\n"
                         "  - { id: a, type: tool.不存在, params: {}, side_effect: none }\n"),
        ]
        for name, yaml_text in bad_cases:
            r3 = client.post("/api/workflows/to_save",
                             json={"yaml_text": yaml_text, "overwrite": True})
            body = r3.json()
            c.check(f"★★ {name} → **拒绝**", body.get("ok") is False, str(body)[:90])
            c.check(f"   {name} → **文件没有被创建**", not target.exists())

        # id 与文件名不一致
        r4 = client.post("/api/workflows/to_save",
                         json={"yaml_text": GOOD.replace("guard_wf", "other_id"),
                               "overwrite": True})
        c.check("★★ YAML 内的 id 与文件名不一致 → 拒绝",
                r4.json().get("ok") is False and "一致" in r4.json().get("error", ""),
                str(r4.json())[:90])

        # --------------------------------------------------------------
        c.section("第 6 节：P4 保存 —— 合法则写入，且能被读回")
        # --------------------------------------------------------------
        r5 = client.post("/api/workflows/to_save",
                         json={"yaml_text": GOOD.replace("guard_wf", "to_save"),
                               "overwrite": True})
        c.check("合法 YAML → 保存成功", r5.json().get("ok") is True, str(r5.json())[:90])
        c.check("★ 文件确实写了", target.exists())
        ids2 = [w["id"] for w in client.get("/api/workflows").json().get("workflows", [])]
        c.check("★ 保存后立即出现在列表里", "to_save" in ids2, str(ids2))

        # overwrite=false 不能覆盖
        before = target.read_text(encoding="utf-8")
        r6 = client.post("/api/workflows/to_save",
                         json={"yaml_text": GOOD.replace("guard_wf", "to_save")
                               .replace("q }", "q2 }"), "overwrite": False})
        c.check("★ overwrite=false → 拒绝覆盖", r6.json().get("ok") is False,
                str(r6.json())[:80])
        c.check("★ 原文件内容未被改动", target.read_text(encoding="utf-8") == before)

        # --------------------------------------------------------------
        c.section("第 7 节：★ 路径穿越被挡住")
        # --------------------------------------------------------------
        c.check("★ `_safe_wf_id` 剥掉路径分隔符",
                "/" not in W._safe_wf_id("../../etc/passwd"))
        c.check("★ 含 `..` 的 id 返回空串（调用方据此拒绝）",
                W._safe_wf_id("..") == "")

        # --------------------------------------------------------------
        c.section("第 8 节：YAML 端点 —— 能取回原文（P4 编辑需要）")
        # --------------------------------------------------------------
        r7 = client.get("/api/workflows/to_save/yaml").json()
        c.check("★ 能取回 YAML 原文", "to_save" in r7.get("yaml_text", ""))
        c.check("不存在的 id → 返回 error 而不是 500",
                "error" in client.get("/api/workflows/nope/yaml").json())

    finally:
        W._workflows_dir = orig_wf_dir                            # type: ignore[assignment]
        _store.runs_dir = orig_runs_dir                           # type: ignore[assignment]
        shutil.rmtree(SANDBOX, ignore_errors=True)

    print()
    print("=" * 78)
    print(f"运行视图与编辑守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


class _StubKB:
    def search(self, q, top_k=3):                                 # noqa: ANN001
        return [{"text": f"stub:{q}"}]


if __name__ == "__main__":
    raise SystemExit(main())