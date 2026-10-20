# -*- coding: utf-8 -*-
"""分级网关 —— 不是每个问题都值得跑完整管线。

【要解决的问题】「你好」也要付一次向量检索 + 一次 rerank + 一次 L4 记忆召回；
而「画个价格走势图」根本不需要查知识库。这些开销本可以省掉。

【★ 设计要点：白名单式准入，默认落到 L2】

  分级网关的真正风险是**误降级** —— 把"该检索的问题"直接答了。
  那种错误**用户无从察觉**（他会以为助手就这点水平），而省下的钱远不足以补偿。

  因此判据取「**白名单式准入**」而不是"黑名单式排除"：
    · 只有**明确命中**才降级；
    · **没把握一律付全价**（走 L2，与改动前逐字一致）。

  这也是 `tests/check_query_router.py` 里那条**反向断言**的由来：
  「含农事实体或工具触发词的问句**绝不**被判为 L0」。

【零 LLM】纯关键词与规则判定，与 `trigger_hits` / `classify_scope` 同一哲学：
确定性、可单测、不花一分钱。

【地区名单一真相】复用 `core/rag_engine._L4_REGION_TOKENS`（**延迟导入**避免循环依赖），
不在本模块另建一份 —— 两份地区清单必然会漂移。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- 档位 ----
L0 = "L0"   # 直答：不检索、不下发工具、不召回记忆
L1 = "L1"   # 轻量：只跳过「检索」或「工具」其中一项
L2 = "L2"   # 全链：与改动前逐字一致的现行行为

# ---- 白名单①：寒暄与身份（L0 的**必要条件**，不是充分条件）----
GREETING_TOKENS: tuple[str, ...] = (
    "你好", "您好", "哈喽", "哈啰", "在吗", "hi", "hello", "hey",
    "你是谁", "你叫什么", "你能做什么", "你能干什么", "你会什么", "你是什么",
    "谢谢", "感谢", "多谢", "再见", "拜拜", "晚安", "早上好", "中午好", "晚上好",
)

# ---- 工具触发词（每类工具一组）----
TOOL_TOKENS: dict[str, tuple[str, ...]] = {
    "get_weather": ("天气", "气温", "温度", "下雨", "降雨", "打药", "喷药", "施药",
                    "风力", "湿度", "霜冻", "预报"),
    "web_search": ("搜索", "搜一下", "上网查", "网上查", "最新消息", "新闻",
                   "政策", "补贴", "公告"),
    "query_cotton_stats": ("统计", "种植面积", "播种面积", "总产", "单产",
                           "产量是多少", "多少亩", "产量数据"),
    "calc_price_volatility": ("价格", "波动", "涨跌", "行情", "多少钱", "价位", "收购价"),
    "plot_trend": ("画图", "绘图", "画个", "画一下", "走势图", "趋势图", "对比图",
                   "图表", "折线图", "柱状图"),
    "analyze_yield": ("预测", "风险评估", "风险等级", "风险分级", "能收多少", "产区风险"),
}

# ---- 检索意图词（问了这些，多半要查知识库）----
RETRIEVAL_TOKENS: tuple[str, ...] = (
    "为什么", "怎么", "如何", "是什么", "什么是", "哪些", "有什么", "有啥",
    "防治", "症状", "原因", "办法", "措施", "技术", "方法", "介绍", "讲讲",
    "说说", "区别", "注意", "要点", "标准", "用量", "配比", "时期",
)

# ---- 农事实体（作物 / 农事动词；地区名从 rag_engine 复用）----
CROP_TOKENS: tuple[str, ...] = (
    "棉花", "棉田", "棉农", "籽棉", "皮棉", "长绒棉", "细绒棉", "机采棉",
    "品种", "苗期", "蕾期", "花铃期", "吐絮", "打顶", "整枝", "滴灌", "漫灌",
    "施肥", "追肥", "底肥", "农药", "杀虫", "除草", "病害", "虫害", "红蜘蛛",
    "蚜虫", "棉铃虫", "枯黄萎", "出苗", "播种", "采收", "采摘", "测产", "亩产",
)

# L0 的长度上限：超过就一定不是寒暄
MAX_L0_CHARS = 20


@dataclass(frozen=True)
class RouteDecision:
    """一次路由判定（含**原因**，便于日志与守卫诊断）。"""

    level: str
    reason: str
    hits: tuple[str, ...] = ()

    @property
    def skip_retrieval(self) -> bool:
        """是否跳过知识库检索（省一次 embedding + 一次 rerank）。"""
        return self.level == L0 or (self.level == L1 and "tool" in self.hits)

    @property
    def skip_tools(self) -> bool:
        """是否不下发工具 schema（省 token，且避免误调）。"""
        return self.level == L0 or (self.level == L1 and "retrieval" in self.hits)

    @property
    def skip_memory(self) -> bool:
        """是否跳过 L4 记忆召回（省一次 embedding）。只有 L0 跳。"""
        return self.level == L0


def _region_tokens() -> tuple[str, ...]:
    """地区名清单 —— 复用 `rag_engine` 的单一真相（**延迟导入**以避免循环）。"""
    try:
        from core.rag_engine import _L4_REGION_TOKENS
        return tuple(_L4_REGION_TOKENS)
    except Exception:                                    # noqa: BLE001
        # 导入失败时**退化为按作物/农事词判定**（保守方向：少判出实体 = 更少降级）
        return ()


def matched_tool_kinds(question: str) -> set[str]:
    """问句命中了哪几类工具。"""
    q = (question or "").strip().lower()
    return {name for name, toks in TOOL_TOKENS.items()
            if any(t.lower() in q for t in toks)}


def has_farm_entity(question: str) -> bool:
    """问句是否提到农事实体（作物 / 农事动词 / 地区名）。"""
    q = (question or "").strip().lower()
    if any(t.lower() in q for t in CROP_TOKENS):
        return True
    return any(t.lower() in q for t in _region_tokens())


def has_retrieval_intent(question: str) -> bool:
    q = (question or "").strip().lower()
    return any(t.lower() in q for t in RETRIEVAL_TOKENS)


def classify(question: str) -> RouteDecision:
    """判定档位。**默认 L2** —— 只有明确命中白名单才降级。"""
    q = (question or "").strip()
    if not q:
        return RouteDecision(L2, "空问句 → 保守走全链")

    lowered = q.lower()
    greeting = [t for t in GREETING_TOKENS if t.lower() in lowered]
    tools = matched_tool_kinds(q)
    entity = has_farm_entity(q)
    retrieval = has_retrieval_intent(q)

    # ---- L0 准入：寒暄命中，且**四类信号一个都不能有** ----
    if greeting:
        blockers = []
        if tools:
            blockers.append(f"工具信号 {sorted(tools)}")
        if entity:
            blockers.append("农事实体")
        if retrieval:
            blockers.append("检索意图")
        if len(q) > MAX_L0_CHARS:
            blockers.append(f"长度 {len(q)}>{MAX_L0_CHARS}")
        if not blockers:
            return RouteDecision(L0, f"寒暄命中 {greeting[0]!r} 且无其他信号", ("greeting",))
        return RouteDecision(L2, "寒暄但含 " + " / ".join(blockers),
                             tuple(["greeting", *([] if not tools else ["tool"])]))

    # ---- L1 准入：只命中"工具"或"检索"**其中一类** ----
    if tools and not retrieval and not entity:
        return RouteDecision(L1, f"仅工具信号 {sorted(tools)}（无检索意图、无农事实体）", ("tool",))
    if tools and not retrieval and entity:
        # 含农事实体但仍只判工具型：只有当**没有检索意图**时才允许
        return RouteDecision(L1, f"工具信号 {sorted(tools)} + 实体但无检索意图", ("tool",))
    if retrieval and not tools:
        return RouteDecision(L1, f"仅检索意图（无工具信号）", ("retrieval",))

    # ---- 其余一律 L2 ----
    if tools and retrieval:
        return RouteDecision(L2, f"工具与检索信号同时出现（{sorted(tools)}）",
                             ("tool", "retrieval"))
    return RouteDecision(L2, "无任何明确信号 → 保守走全链")


def describe(decision: RouteDecision) -> str:
    """一行诊断（供 `[L1 组装]` 风格日志）。"""
    return (f"路由 {decision.level}（{decision.reason}）| "
            f"跳过 检索={'是' if decision.skip_retrieval else '否'} "
            f"工具={'是' if decision.skip_tools else '否'} "
            f"记忆={'是' if decision.skip_memory else '否'}")