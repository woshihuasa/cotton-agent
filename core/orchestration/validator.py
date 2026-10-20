# -*- coding: utf-8 -*-
"""工作流静态校验 —— **一个字节都不执行，就能把大部分错误挡下来**。

【为什么这层最值钱】它把"跑起来才发现流程写错了"变成"**保存时就告诉你**"。
        而且全部是纯静态检查：零 LLM、零网络、零磁盘写入，可以进 CI、可以每次保存都跑。

【校验什么】（对应 `docs/ORCHESTRATION.md` §6.1 的 5 类反例）
        1. **未知节点类型** —— 拼错了 / 已设计但未实现（**两者分开报**）
        2. **重复 id**
        3. **悬空依赖**（`needs` 指向不存在的节点）
        4. **环**（拓扑排序走不完）
        5. **必填参数缺失**（按 `catalog` 的契约查）
        6. **未知参数名**（契约里没有的参数 —— 防拼写错误被静默忽略）
        7. **孤立节点**（既不依赖别人、也没人依赖它，且不是唯一节点）
        8. **未声明副作用的写盘节点**（保守默认生效，但**要求显式声明** —— 见下）
        9. **非幂等节点却写了 retry > 0**（会被静默取 0，所以**报出来让人知道**）

【"未声明副作用"为什么不直接判错、但必须报】
        `model.DEFAULT_SIDE_EFFECT` 是 `disk`（保守），所以未声明**不会**导致误重试。
        但"写了但没生效"和"没写"是两件事，静默取 0 会让人以为自己配了重试。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.orchestration import catalog
from core.orchestration.model import (
    RETRIABLE_SIDE_EFFECTS,
    SideEffect,
    Workflow,
)


class Level:
    """问题等级。`ERROR` 必须修；`WARN` 能跑但建议修。"""

    ERROR = "error"
    WARN = "warn"


@dataclass(frozen=True)
class Issue:
    """一条校验问题。`node_id` 为空表示问题出在整份工作流层面。"""

    level: str
    code: str
    message: str
    node_id: str = ""

    def __str__(self) -> str:
        where = f"[{self.node_id}] " if self.node_id else ""
        return f"{self.level.upper():5s} {self.code:22s} {where}{self.message}"


def _err(code: str, message: str, node_id: str = "") -> Issue:
    return Issue(Level.ERROR, code, message, node_id)


def _warn(code: str, message: str, node_id: str = "") -> Issue:
    return Issue(Level.WARN, code, message, node_id)


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------


def _check_types(wf: Workflow, issues: list[Issue]) -> None:
    """未知/未实现的节点类型。"""
    for n in wf.nodes:
        if catalog.get(n.type) is None:
            issues.append(_err("unknown-node-type", catalog.describe(n.type), n.id))
            continue
        if n.type not in catalog.EXECUTABLE_TYPES and not n.type.startswith("tool."):
            issues.append(_err(
                "not-executable",
                f"{n.type} 已在文档中设计但当前不可执行", n.id))


def _check_duplicate_ids(wf: Workflow, issues: list[Issue]) -> None:
    seen: dict[str, int] = {}
    for n in wf.nodes:
        seen[n.id] = seen.get(n.id, 0) + 1
    for nid, cnt in seen.items():
        if cnt > 1:
            issues.append(_err("duplicate-id", f"节点 id 重复出现 {cnt} 次", nid))


def _check_needs(wf: Workflow, issues: list[Issue]) -> None:
    """悬空依赖 + 自依赖。"""
    ids = set(wf.ids())
    for n in wf.nodes:
        for dep in n.needs:
            if dep == n.id:
                issues.append(_err("self-dependency", "节点依赖了自己", n.id))
            elif dep not in ids:
                issues.append(_err(
                    "dangling-need", f"needs 指向不存在的节点 {dep!r}", n.id))


def _check_cycles(wf: Workflow, issues: list[Issue]) -> None:
    """环检测（Kahn 拓扑排序；走不完 = 有环）。

    ★ 报出**环路本身**而不是只说"有环" —— 否则节点几十个时根本找不到问题在哪。
    """
    ids = set(wf.ids())
    indeg = {i: 0 for i in ids}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for n in wf.nodes:
        for dep in n.needs:
            if dep in ids and dep != n.id:
                adj[dep].append(n.id)
                indeg[n.id] += 1

    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if seen != len(ids):
        stuck = sorted(i for i, d in indeg.items() if d > 0)
        issues.append(_err(
            "cycle",
            f"存在环：以下节点无法完成拓扑排序 → {stuck}"))


def _check_params(wf: Workflow, issues: list[Issue]) -> None:
    """必填缺失 + 未知参数名。"""
    for n in wf.nodes:
        spec = catalog.get(n.type)
        if spec is None:
            continue                                     # 类型错已在别处报过
        given = set(n.params)
        allowed = set(spec.param_names())
        missing = [p for p in spec.required_params() if p not in given]
        if missing:
            issues.append(_err(
                "missing-params",
                f"{n.type} 缺少必填参数 {missing}（契约要求: {sorted(allowed)}）", n.id))
        unknown = given - allowed
        if unknown:
            # ★ 未知参数名**必须报错** —— 静默忽略等于"写了但没生效"
            issues.append(_err(
                "unknown-params",
                f"{n.type} 不认识参数 {sorted(unknown)}；可用: {sorted(allowed)}", n.id))


def _check_side_effects(wf: Workflow, issues: list[Issue]) -> None:
    """副作用声明相关：未声明（保守生效但要求显式）、非幂等却写 retry。"""
    for n in wf.nodes:
        if n.side_effect is None:
            issues.append(_warn(
                "side-effect-undeclared",
                f"{n.type} 未声明 side_effect —— 按保守默认"
                f"（{SideEffect.DISK.value}）处理，因此**不可重试**。"
                f"若它其实只读/纯计算，请显式写 `side_effect: none`", n.id))
        elif n.retry > 0 and n.effective_side_effect not in RETRIABLE_SIDE_EFFECTS:
            issues.append(_warn(
                "retry-ignored",
                f"写了 retry={n.retry}，但 {n.effective_side_effect.value} 副作用"
                f"**不是幂等的**，实际会被取 0（不重试）", n.id))


def _check_orphans(wf: Workflow, issues: list[Issue]) -> None:
    """孤立节点：既无入边也无出边，且工作流不止一个节点。

    单节点的合法工作流（比如只有一次 `data.kb_query`）不该被报，所以先判节点数。
    """
    if len(wf.nodes) <= 1:
        return
    has_edge = set()
    for a, b in wf.edges():
        has_edge.add(a)
        has_edge.add(b)
    for n in wf.nodes:
        if n.id not in has_edge:
            issues.append(_warn(
                "orphan-node",
                "孤立节点：不依赖任何人、也没人依赖它（可能是漏写了 needs）", n.id))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def validate(wf: Workflow) -> list[Issue]:
    """跑全部检查，返回问题列表（**已排序**：错误在前，按节点 id）。"""
    issues: list[Issue] = []
    _check_duplicate_ids(wf, issues)
    _check_types(wf, issues)
    _check_needs(wf, issues)
    _check_cycles(wf, issues)
    _check_params(wf, issues)
    _check_side_effects(wf, issues)
    _check_orphans(wf, issues)
    issues.sort(key=lambda i: (i.level != Level.ERROR, i.node_id, i.code))
    return issues


def errors_only(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == Level.ERROR]


def is_valid(issues: list[Issue]) -> bool:
    """**只有 ERROR 才算不合法**；WARN 不阻断加载。"""
    return not errors_only(issues)


def format_issues(issues: list[Issue]) -> str:
    """把问题列表压成可读文本（界面/日志用）。空列表返回 `"OK"`。"""
    if not issues:
        return "OK"
    return "\n".join(str(i) for i in issues)


__all__ = [
    "Issue", "Level", "errors_only", "format_issues", "is_valid", "validate",
]