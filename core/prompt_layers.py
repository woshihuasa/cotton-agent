# -*- coding: utf-8 -*-
"""提示分层与稳定前缀指纹 —— 让「上下文缓存命中」从猜测变成可测量、可守卫的事实。

【背景】DeepSeek 的上下文缓存按 **prompt 前缀逐字节相同** 判定命中，而命中 token 的
单价显著低于未命中。因此 **prompt 的段序直接决定成本**：稳定内容必须在前，易变内容必须在后。

【实测发现（2026-10）】本项目的 `SYSTEM_PROMPT`（`core/rag_engine.py`）
**已经天然满足**这个前提 —— `today_date` 与约 54 行固定规则（工具规则 + 五条质量约束）在前，
四个可变段全部在末尾。所以本模块**不是"重构 prompt"**，而是把它变成**可测量、可守卫的事实**：

  · `stable_hash` —— 把稳定段的字节内容压成一个指纹，写进每轮日志
  · 守卫 —— 断言**同一日期内 `stable_hash` 恒定**：任何"把可变内容插进前缀"的改动
    都会被当场抓住，而不是等到某天发现成本变高了才回头查

【为什么只测量、不重建】现有组装走 `SYSTEM_PROMPT.format(...)`，**其产出字节已经适合缓存**。
若为了"分层"而改写组装逻辑，反而可能改变字节、把已经命中的前缀打散 ——
**测量不该有副作用**。故本模块只读模板、只算指纹，不参与拼装。

【为什么用"指纹"而不是"比较字符串"】指纹能进日志、进 trace、做分组统计
（同一 `stable_hash` 下的成功率对比才有意义），而整串字符串不能。
"""
from __future__ import annotations

import hashlib

# 模板里稳定段与易变段的**唯一分界**。
# 分界之后依次是：L3 摘要 → L4 记忆 → 技能 → 背景知识（全部逐轮可变）。
STABLE_BOUNDARY = "{summary_section}"

# 易变段的逻辑名（供分层审计；与实际拼接顺序一致）
VOLATILE_LAYERS: tuple[str, ...] = ("l3_summary", "l4_memory", "skills", "kb_context")


def stable_prefix(template: str, today_date) -> str:
    """模板的**稳定段**（只填入日期，不含任何逐轮可变内容）。

    为什么 `today_date` 算稳定：它**每天只变一次**，同日内的所有请求共享同一前缀。
    这与"逐轮可变"有本质区别 —— 后者会让缓存每次都失效。
    """
    head = str(template).split(STABLE_BOUNDARY, 1)[0]
    try:
        return head.format(today_date=today_date)
    except (KeyError, IndexError):
        # 模板稳定段里出现了非 today_date 的占位符 —— 如实返回原文，不猜
        return head


def stable_hash(template: str, today_date) -> str:
    """稳定段的 sha256 指纹（64 位十六进制）。

    进日志与 trace；**同一日期内必须恒定**（由守卫断言）。
    """
    return hashlib.sha256(stable_prefix(template, today_date).encode("utf-8")).hexdigest()


def has_boundary(template: str) -> bool:
    """模板是否含分层边界。守卫用它防止"边界被误删导致分层静默失效"。"""
    return STABLE_BOUNDARY in str(template)


def verify_prefix(system_content: str, template: str, today_date) -> bool:
    """实测不变量：**组装出的 system prompt 必须以稳定段开头**。

    这是"前缀缓存有效"的**充要条件在代码里的直接表达** ——
    若某天有人在稳定段中间插了一句逐轮变化的话，本断言立刻为假。
    """
    return str(system_content).startswith(stable_prefix(template, today_date))


def layer_sizes(template: str, *, today_date,
                summary_section: str = "", l4_section: str = "",
                skill_section: str = "", context: str = "") -> dict[str, int]:
    """各层字符数（供分层审计；不参与拼装）。

    顺序与真实拼接一致：稳定段 → L3 → L4 → 技能 → 背景知识。
    """
    return {
        "stable": len(stable_prefix(template, today_date)),
        "l3_summary": len(summary_section or ""),
        "l4_memory": len(l4_section or ""),
        "skills": len(skill_section or ""),
        "kb_context": len(context or ""),
    }


def describe_layer_sizes(sizes: dict[str, int]) -> str:
    """把分层审计压成一行（供 `[L1 组装]` 风格日志）。"""
    total = sum(sizes.values()) or 1
    parts = [f"{name} {n}({n / total:.0%})" for name, n in sizes.items() if n]
    return f"共 {total} 字 | " + " · ".join(parts)


#: 「无缓存统计」时显示的占位符。
#: ★ 为什么不用 "0%"：`Usage.cache_hit_rate` 在**没有数据**时也返回 `0.0`，
#: 于是"接口没回传字段"和"真的全部未命中"在数值上无法区分。
#: **混为一谈会让读者以为缓存失效了** —— 而实际可能只是接口没给这个字段。
CACHE_HINT_NO_DATA = "缓存命中 —"


def format_cache_hint(hit: int, miss: int, model: str = "") -> tuple[str, str]:
    """把缓存命中数压成「输入框下方那一行小字」+ 悬停明细。

    返回 `(显示文本, 悬停提示)`。这是**纯函数**，便于零成本守卫测试
    （UI 里只负责把返回值贴到 QLabel 上）。

    · 有数据：`缓存命中 83%`，悬停显示 `提示词 1234 token：命中 1024 / 未命中 210`
    · 无数据：`缓存命中 —`，悬停**明确说明**"未回传统计，不代表 0%"
    """
    hit = max(0, int(hit or 0))
    miss = max(0, int(miss or 0))
    if not (hit or miss):
        return CACHE_HINT_NO_DATA, "本轮接口未回传缓存统计（不代表命中率为 0）"
    total = hit + miss
    rate = hit / total * 100
    text = f"缓存命中 {rate:.0f}%"
    tip = f"提示词 {total} token：命中 {hit} / 未命中 {miss}"
    if model:
        tip += f"\n模型：{model}"
    return text, tip