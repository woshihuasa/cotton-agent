# -*- coding: utf-8 -*-
"""上下文双层预算 —— 逐源硬上限 + 全局代价最小化。

【为什么需要】L1 组装的上下文有**五个来源**（L3 摘要 / L4 记忆 / 结晶档 / 手写技能 / 背景知识）。
在本模块之前，只有后两者中的一部分有字符上限，而 **L3 摘要、L4 记忆、背景知识三个完全没有上限**，
并且**没有全局预算** —— 于是"prompt 会多长"不可预测，也没有任何东西能回答
"这一轮到底哪个来源占了大头、谁被截了、谁被丢了"。

【两级结构】（照 LeAgent `context/budget.py` 的思路）

  第一级 **逐源硬上限**：每个源先各自截断，保证**单源不吞掉整个窗口**。
  第二级 **全局代价最小化**：剩余预算按 `priority / cost` 贪心填充，确定性 tie-break。

  ⚠️ 顺序不能反：先逐源、再全局。否则"某个巨大来源"会先把全局预算吃光，
  后面小来源连参与排序的机会都没有。

【审计行是重点】`BudgetRow` 逐源记录 `original_cost / final_cost / kept / truncated / dropped`。
它把"prompt 为什么这么长"从猜测变成**可复盘的事实** —— 这是本模块最值得做的部分。

【单一真相】结晶档与技能的上限**也在这里定义**（`SOURCE_HARD_CAPS`），
`procedural_memory` 与 `skill_loader` 从这里取值，不再各自写死一份。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---- 逐源硬上限（字符）----
# ⚠️ **分层说明（一处与原方案的偏离，已记入 docs/AGENT_RUNTIME.md）**：
# 原计划把「结晶档 300 / 技能 600」两个常量也收敛进本表。但实际分层里，
# `rag_engine` 拿到的是**已合并的 `skill_section`**（结晶档 + 技能串在一个 prompt 槽里），
# 而那两个上限是**各自模块内部**的明细预算（`format_section` 的"正文→摘要→停止"回落）。
# 硬把两者塞进同一张表会让"模块内预算"与"组装层预算"两个层次混在一起。
# 故本表**只约束组装层的四个段落**，`skill_slot` 的上限取两者之和。
#
# 取值原则：**宁可宽松**。上限是"防单源失控"的护栏，不是"压榨 token"的闸门 ——
# 截断会伤及答案，所以起步值取得比较宽，等审计行积累了真实分布再收紧。
SOURCE_HARD_CAPS: dict[str, int] = {
    "l3_summary": 2000,      # 【历史摘要】+【当前会话状态】
    "l4_memory": 1000,       # 【用户长期记忆】
    "skill_slot": 900,       # 【历史处理路径】+【技能 · 编排指导】= 结晶档 300 + 技能 600
    "kb_context": 3000,      # 【背景知识】
}

# 全局预算（所有易变来源之和的上限）。默认取得比上限之和更宽 ——
# 它是一张**安全网**，不是紧箍咒（上限之和 = 6900 < 8000）。
DEFAULT_TOTAL_BUDGET_CHARS = 8000

# 各源的默认优先级（越大越先保）。数值只用于排序，绝对值无意义。
SOURCE_PRIORITY: dict[str, int] = {
    "l3_summary": 80,        # 最近状态最相关
    "l4_memory": 70,         # 长期记忆
    "kb_context": 60,        # 知识库（有 rerank 撑腰，质量高）
    "skill_slot": 50,        # 结晶档 + 人写技能
}

TRUNCATION_MARK = "\n…[超出预算已截断]"


@dataclass
class SourceBlock:
    """一个上下文来源（预算层的最小单位）。"""

    source_id: str
    text: str
    priority: int = 50
    pinned: bool = False       # True = 任何情况下都不丢（如系统规则）

    @property
    def cost(self) -> int:
        return len(self.text or "")


@dataclass
class BudgetRow:
    """逐源审计行（kept / truncated / dropped 三者互斥且**必须被记录**）。"""

    source_id: str
    original_cost: int
    final_cost: int
    priority: int
    kept: bool
    truncated: bool = False
    dropped: bool = False

    def describe(self) -> str:
        if self.dropped:
            flag = "丢弃"
        elif self.truncated:
            flag = f"截断 {self.original_cost}→{self.final_cost}"
        elif self.kept:
            flag = f"保留 {self.final_cost}"
        else:
            flag = "空"
        return f"{self.source_id}:{flag}"


@dataclass
class BudgetResult:
    blocks: list[SourceBlock] = field(default_factory=list)
    rows: list[BudgetRow] = field(default_factory=list)

    @property
    def total_cost(self) -> int:
        return sum(b.cost for b in self.blocks)

    def text_of(self, source_id: str) -> str:
        for b in self.blocks:
            if b.source_id == source_id:
                return b.text
        return ""

    def describe(self) -> str:
        """一行审计（供 `[L1 预算]` 风格日志）。"""
        if not self.rows:
            return "无来源"
        return (f"共 {self.total_cost} 字 | "
                + " · ".join(r.describe() for r in self.rows))


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """按上限截断（保留头部 + 标记）。返回 (文本, 是否发生截断)。"""
    if cap <= 0:
        return "", bool(text)
    if len(text) <= cap:
        return text, False
    if cap <= len(TRUNCATION_MARK):
        return text[:cap], True
    return text[: cap - len(TRUNCATION_MARK)] + TRUNCATION_MARK, True


def apply_caps(blocks: list[SourceBlock],
               caps: dict[str, int] | None = None) -> BudgetResult:
    """第一级：逐源硬上限。每个源独立截断，**互不影响**。"""
    caps = SOURCE_HARD_CAPS if caps is None else caps
    out: list[SourceBlock] = []
    rows: list[BudgetRow] = []
    for b in blocks:
        cap = caps.get(b.source_id)
        if cap is None:
            # 未声明上限的来源：**不截断**（保守：宁可长，不可误伤）
            new_text, cut = b.text, False
        else:
            new_text, cut = _truncate(b.text or "", cap)
        out.append(SourceBlock(b.source_id, new_text, b.priority, b.pinned))
        rows.append(BudgetRow(
            source_id=b.source_id, original_cost=b.cost, final_cost=len(new_text),
            priority=b.priority, kept=bool(new_text), truncated=cut,
        ))
    return BudgetResult(out, rows)


def minimise(blocks: list[SourceBlock], total_budget: int) -> BudgetResult:
    """第二级：全局代价最小化（贪心）。

    排序键 `priority / cost` 降序 → 同分按 `source_id` 升序（**确定性**，结果可复现）。
    `pinned` 的来源**先无条件保留**，不参与竞争。
    """
    kept: list[SourceBlock] = []
    rows: list[BudgetRow] = []
    used = 0

    for b in blocks:
        if b.pinned:
            kept.append(b)
            used += b.cost
            rows.append(BudgetRow(b.source_id, b.cost, b.cost, b.priority, kept=True))

    rest = [b for b in blocks if not b.pinned and b.cost > 0]
    # 空块也要出行（否则"某源静默消失"无法被发现）
    empties = [b for b in blocks if not b.pinned and b.cost == 0]
    for b in empties:
        rows.append(BudgetRow(b.source_id, 0, 0, b.priority, kept=False))

    def _key(b: SourceBlock):
        # cost 为 0 的已被排除；priority/cost 越大越优先
        return (-(b.priority / max(1, b.cost)), b.source_id)

    remaining = max(0, total_budget - used)
    for b in sorted(rest, key=_key):
        if b.cost <= remaining:
            kept.append(b)
            remaining -= b.cost
            rows.append(BudgetRow(b.source_id, b.cost, b.cost, b.priority, kept=True))
        else:
            rows.append(BudgetRow(b.source_id, b.cost, 0, b.priority,
                                  kept=False, dropped=True))

    # 行顺序固定为"输入顺序"，便于逐轮对比
    order = {b.source_id: i for i, b in enumerate(blocks)}
    rows.sort(key=lambda r: order.get(r.source_id, 999))
    kept.sort(key=lambda b: order.get(b.source_id, 999))
    return BudgetResult(kept, rows)


def fit(blocks: list[SourceBlock],
        total_budget: int = DEFAULT_TOTAL_BUDGET_CHARS,
        caps: dict[str, int] | None = None) -> BudgetResult:
    """两级一次做完：**先逐源截断，再全局最小化**（顺序不可颠倒）。"""
    capped = apply_caps(blocks, caps)
    result = minimise(capped.blocks, total_budget)
    # 合并两级的信息：截断标记不能被全局层抹掉
    cap_rows = {r.source_id: r for r in capped.rows}
    for r in result.rows:
        prev = cap_rows.get(r.source_id)
        if prev is not None and prev.truncated and r.kept:
            r.truncated = True
            r.original_cost = prev.original_cost
    return result