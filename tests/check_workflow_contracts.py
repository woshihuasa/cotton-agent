# -*- coding: utf-8 -*-
"""工作流**契约一致性**守卫 —— `catalog` 派生的 `tool.*` 与 `schemas.py` 必须逐字段一致。

【定位】`docs/ORCHESTRATION.md` §6.1 的第 1 个守卫（P0）。

        **这条防线防的是"契约抄两份"** —— 本项目在 `07` 号文档里已经总结过：
        "**两份地图必然对不上**"。所以编排层**不抄**工具参数定义，而是从
        `core/tools/schemas.py` 派生；本守卫断言派生结果**没有漂移**。

【零 LLM / 零 API】纯静态比对。

【用法】python tests/check_workflow_contracts.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.orchestration import catalog                      # noqa: E402
from core.orchestration.model import SideEffect             # noqa: E402
from core.tools import TOOL_HANDLERS, TOOL_SCHEMAS          # noqa: E402
from core.tools import capabilities as tool_caps            # noqa: E402


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


def _schema_view(tool_schema: dict) -> tuple[str, list[str], list[str]]:
    fn = tool_schema.get("function") or {}
    params = fn.get("parameters") or {}
    props = sorted((params.get("properties") or {}).keys())
    required = sorted(params.get("required") or [])
    return str(fn.get("name") or ""), props, required


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：★★★ 派生的 tool.* 与 schemas.py **逐字段一致**")
    # ------------------------------------------------------------------
    c.check("schemas.py 里有工具", len(TOOL_SCHEMAS) > 0, str(len(TOOL_SCHEMAS)))
    for tool_schema in TOOL_SCHEMAS:
        name, props, required = _schema_view(tool_schema)
        node_type = f"tool.{name}"
        spec = catalog.get(node_type)
        if spec is None:
            c.check(f"★ {node_type} 在目录里存在", False, "目录里没有这个节点类型")
            continue
        c.check(f"★ {node_type} 参数集合一致（逐字段）",
                sorted(spec.param_names()) == props,
                f"目录 {sorted(spec.param_names())} vs schema {props}")
        c.check(f"★ {node_type} 必填集合一致",
                sorted(spec.required_params()) == required,
                f"目录 {sorted(spec.required_params())} vs schema {required}")

    # 反向：目录里不许有 schema 里没有的 tool.* 节点
    schema_types = {f"tool.{_schema_view(t)[0]}" for t in TOOL_SCHEMAS}
    extra = set(catalog.tool_node_types()) - schema_types
    c.check("★★ 目录里**没有** schema 之外的 tool.* 节点（双向完整性）",
            not extra, f"多出: {sorted(extra)}")

    # ------------------------------------------------------------------
    c.section("第 2 节：副作用由**能力注解**派生（不另造判据）")
    # ------------------------------------------------------------------
    for name in sorted(TOOL_HANDLERS):
        spec = catalog.get(f"tool.{name}")
        cap = tool_caps.CAPABILITIES.get(name)
        if spec is None or cap is None:
            c.check(f"{name} 有目录项与能力注解", False)
            continue
        if cap.read_only and not cap.destructive:
            want = SideEffect.NETWORK if cap.open_world else SideEffect.NONE
        else:
            want = SideEffect.DISK
        c.check(f"{name}: read_only={cap.read_only} open_world={cap.open_world} → {want.value}",
                spec.side_effect == want, f"实际 {spec.side_effect.value}")

    c.check("★ 有副作用（写盘）的工具被识别为 disk",
            catalog.get("tool.plot_trend").side_effect == SideEffect.DISK)
    c.check("★ 只读本地的工具是 none（可重试）",
            catalog.get("tool.query_cotton_stats").side_effect == SideEffect.NONE)
    c.check("★ 联网工具是 network",
            catalog.get("tool.web_search").side_effect == SideEffect.NETWORK)

    # ------------------------------------------------------------------
    c.section("第 3 节：双向完整性 —— 目录 ↔ 实现表")
    # ------------------------------------------------------------------
    # 有目录项却没有实现的工具，会在执行期才炸 —— 静态就该拦
    no_impl = [t for t in catalog.tool_node_types()
               if t.split(".", 1)[1] not in TOOL_HANDLERS]
    c.check("★ 每个 tool.* 节点都有对应实现（否则执行期才炸）",
            not no_impl, f"缺实现: {no_impl}")
    no_type = [h for h in TOOL_HANDLERS if catalog.get(f"tool.{h}") is None]
    c.check("★ 每个工具实现都有对应节点类型（否则编排层用不到它）",
            not no_type, f"缺节点类型: {no_type}")

    # ------------------------------------------------------------------
    c.section("第 4 节：非工具节点的契约自洽")
    # ------------------------------------------------------------------
    for t in sorted(catalog.EXECUTABLE_TYPES):
        spec = catalog.get(t)
        c.check(f"{t} 有目录项", spec is not None)
        if spec is not None:
            c.check(f"{t} 的 side_effect 已显式声明（不是缺省兜底）",
                    spec.side_effect in tuple(SideEffect),
                    str(spec.side_effect))
    # ★ 原来的断言是「所有 gate.* 都是 none」—— 那在"全是确定性门禁"时成立，
    #   但 `gate.agent_review` **故意**是 network（它要调模型）。所以改成
    #   **显式例外清单**：谁要是再偷偷加一个花钱的门禁，这里会当场失败。
    PURE_GATES = {"gate.numeric_consistency", "gate.range", "gate.mutual_exclusion",
                  "gate.uncertainty"}
    PAID_GATES = {"gate.agent_review"}
    c.check("★ 确定性门禁全是 none（纯校验，可重试）",
            all(catalog.get(t).side_effect == SideEffect.NONE for t in PURE_GATES))
    c.check("★ 花钱的门禁是 network，且**在例外清单里被显式列出**",
            all(catalog.get(t).side_effect == SideEffect.NETWORK for t in PAID_GATES),
            str({t: catalog.get(t).side_effect.value for t in PAID_GATES}))
    c.check("★★ 没有「清单之外的」gate.*（新增必须同时更新本守卫）",
            {t for t in catalog.known_types() if t.startswith("gate.")}
            == PURE_GATES | PAID_GATES,
            str(sorted(t for t in catalog.known_types() if t.startswith("gate."))))
    c.check("★ `llm.*` 是 network（会调外部接口）",
            all(catalog.get(t).side_effect == SideEffect.NETWORK
                for t in catalog.EXECUTABLE_TYPES if t.startswith("llm.")))

    # ------------------------------------------------------------------
    c.section("第 5 节：★ 未实现的节点类型**不谎报**")
    # ------------------------------------------------------------------
    for t in ("flow.sub", "skill.match", "notify.push", "agent.delegate"):
        c.check(f"{t} **不在**目录里（文档 ⏳，代码零实现）",
                catalog.get(t) is None)
        desc = catalog.describe(t)
        c.check(f"{t} 的报错说明「已设计但不可执行」",
                ("设计" in desc) and ("不可执行" in desc), desc[:80])
    # 拼错的类型要能被区分出来
    typo = catalog.describe("tool.get_wether")
    c.check("★ 拼错的工具名 → 提示同族可用项（与「未实现」区分开）",
            "可用" in typo, typo[:80])

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"工作流契约守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())