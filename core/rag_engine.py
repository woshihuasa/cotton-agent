"""
RAG 引擎模块 (v5 - 多会话管理 + 四层记忆)

整合大模型 (LLMClient)、本地知识库 (KnowledgeBase)、原生工具,
实现检索增强生成 (RAG) 问答流程, 支持联网搜索与天气查询。

工具:
  web_search  — Tavily 搜索 API (tavily-python)
  get_weather — 高德地图天气 API (requests)

记忆架构:
  L1  实时上下文 -> System Prompt 注入 L3 摘要 + L4 实体记忆 + 背景知识
  L2  近期对话   -> 每会话独立 conversation_store (Token 超阈值压缩)
  L3  长期摘要   -> 每会话独立 summary_memory (异步线程生成)
  L4  实体记忆   -> ChromaDB user_memory (全局跨会话共享)

多会话:
  self.sessions: dict[str, Session] — 所有会话状态
  self.current_session_id: str      — 当前活跃会话
"""

import dataclasses
import json
import logging
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Generator, Optional

import requests
import tiktoken

log = logging.getLogger("rag")

from config import AppConfig
from core.cotton_price import (
    calc_percentile,
    calc_pct_change,
    calc_volatility,
    monthly_series,
    query_price,
)
from core.cotton_stats import (
    aggregate,
    calc_mu,
    list_regions,
    list_years,
    query_major,
    query_value,
    rank_by_year,
    series_by_year,
)
from core.knowledge_base import KnowledgeBase
from core.llm_client import LLMClient
from core.trend_plot import generate_trend_chart, next_chart_path
from core.yield_analysis import forecast_yield, risk_ranking

# ---------------------------------------------------------------------------
# 系统提示词常量
# ---------------------------------------------------------------------------

# fmt: off
SYSTEM_PROMPT = (
    "你是一个专业的新疆棉花种植助手. 今天的日期是 {today_date}.\n\n"
    "我将为你提供一些【背景知识】(来自本地知识库) 和【历史摘要】(来自长期记忆), "
    "请按以下原则回答:\n"
    "1. 如果【背景知识】中包含与问题直接相关的信息, 请主要基于背景知识回答, "
    "你可以用自身的专业语言进行润色, 解释和补充, 使回答更通俗易懂.\n"
    "2. 如果【背景知识】中没有相关信息, 或仅有部分相关, "
    '请利用你自身掌握的棉花种植通用知识来回答, 不要回复"无法回答".\n'
    "3. 诚实声明: 如果你的回答主要依赖了自身通用知识而非背景知识, "
    '请在回答末尾附加:"(注: 此回答基于AI通用知识, 建议结合当地农技部门意见验证.)"\n\n'
    "【统计数据工具使用规则】:\n"
    "当你调用统计数据工具(query_cotton_stats)时:\n"
    "1. 工具会自动解析地区名(如\"阿拉尔市\"会自动归入\"生产建设兵团\"口径), "
    "解析结果会在返回中包含 note 字段, 回答时应主动向用户说明该对应关系.\n"
    "2. 如果工具返回 regions_data(候选地区数据列表), 说明输入的行政单位没有单独统计, "
    "应直接把候选地区的数据逐一呈现给用户, 并说明\"该单位无单独统计, 以下为相关地区数据\", "
    "不要要求用户重新输入.\n"
    "3. 如果工具返回 error, 先检查是否可以从 error 信息中的可用年份/地区线索自行修正后重试, "
    "仅在所有尝试都失败后才向用户说明数据不可得.\n\n"
    "【图表工具使用规则】:\n"
    "当用户要求绘制趋势图/对比图/走势图时, 调用 plot_trend 工具.\n"
    "1. 工具返回的 chart_path 是图片文件路径, series 是数据序列(便于你用文字总结).\n"
    "2. 回复格式要求: 先用文字概括趋势要点, 然后在回答的末尾另起一行嵌入图表, "
    "格式必须严格为: ![图表](chart_path 的值) — 例如 ![图表](data/charts/trend_xxx.png).\n"
    "3. 不要编造图片路径, 只使用工具返回的 chart_path; 图片标记必须出现在最终回复中, 否则用户看不到图表.\n\n"
    "【产量分析工具使用规则】:\n"
    "当用户要求预测产量/预测面积/风险评估/风险等级时, 调用 analyze_yield 工具.\n"
    "1. forecast 操作返回预测值/置信区间/趋势方向/R2, 回答时必须注明: "
    "\"该预测基于历史趋势的统计外推, 仅供参考\".\n"
    "2. risk 操作返回产区波动风险分级表, 直接以 Markdown 表格呈现给用户.\n"
    "3. region 参数必须使用用户原话中的地区名, 不要自行替换或联想其他地区.\n\n"
    "{summary_section}"
    "{l4_section}"
    "【背景知识】:\n{context}"
)

REWRITE_SYSTEM_PROMPT = (
    "你是一个查询重写助手. 请根据下方的【对话历史】, "
    "将用户的【最新问题】重写为一个独立, 完整, 无指代代词的问题, "
    "以便于在知识库中进行检索."
    "只需输出重写后的问题本身, 不要回答它, 不要加任何解释."
)

SUMMARY_PROMPT = (
    "请提取以下对话中的关键农事决策信息 (品种, 病虫害, 农药, 施肥, 灌溉, "
    "种植时间, 农田状态, 产量). 用一段简洁的话总结, 不超过 150 字.\n\n"
    "对话内容:\n{history_text}"
)
# fmt: on

L4_EXTRACT_PROMPT = (
    "你是一个农田信息抽取助手。\n\n"
    "请根据当前对话、近期对话历史和长期摘要, "
    "提取关于用户农场的关键事实或偏好。\n"
    "每条事实必须是一个独立的、可验证的陈述句。\n"
    "以 JSON 数组格式输出对象, 每个对象包含 fact 和 category 两个字段:\n"
    '[{"fact": "用户种植新陆早77号", "category": "fact"}, '
    '{"fact": "用户偏好早熟品种", "category": "preference"}]\n'
    'category 取值: "fact"(客观事实) / "preference"(用户偏好) / '
    '"episodic"(事件经历)。\n'
    "不要输出任何解释, 只输出 JSON 数组。\n\n"
    "当前提问: {question}\n"
    "当前回答: {answer}\n"
    "近期对话: {recent_context}\n"
    "长期摘要: {summary_memory}"
)

L4_CONFLICT_PROMPT = (
    "你是一个记忆冲突消解助手。\n\n"
    "给定一条【旧记忆】和一条【新候选事实】, "
    "判断应该执行什么操作。\n\n"
    "判断规则:\n"
    '- ADD: 新旧不冲突且新事实是有效的补充信息。\n'
    '- UPDATE: 新事实更新或纠正了旧记忆 (如品种变了、地址换了)。\n'
    '- DELETE: 旧记忆已过时或与新事实直接矛盾且新事实更可信。\n'
    '- NOOP: 新事实和旧记忆实质相同, 无需改动。\n\n'
    '只输出一个单词: ADD / UPDATE / DELETE / NOOP。\n\n'
    "【旧记忆】: {old_memory}\n"
    "【新候选】: {new_fact}"
)

L3_COMPRESS_PROMPT = (
    "请将以下多段历史摘要合并并二次压缩，"
    "只保留最核心的农田状态、品种特征和关键农事决策。"
    "输出一段不超过300字的最终总结。\n\n"
    "历史摘要:\n{old_summaries}"
)

TITLE_PROMPT = (
    "请根据以下对话的第一轮问答，生成一个简短标题（不超过15个字），"
    "概括对话主题。只输出标题文本，不要加引号和解释。\n\n"
    "用户：{question}\n助手：{answer}"
)


# ---------------------------------------------------------------------------
# Session 数据类
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Session:
    """单个会话的状态数据。

    每个会话拥有独立的 L2 (近期对话) 和 L3 (摘要) 记忆,
    L4 实体记忆和 RAG 知识库则全局共享。
    """
    session_id: str
    title: str
    conversation_store: list
    summary_memory: str
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class RAGEngine:
    """RAG 问答引擎 (v5), 多会话 + 原生工具 + 四层记忆 + Token 动态压缩."""

    MAX_L2_TOKENS = 20000
    TARGET_L2_TOKENS = 10000

    _tokenizer: Optional["tiktoken.Encoding"] = None

    def __init__(self) -> None:
        self.llm = LLMClient()
        self.kb = KnowledgeBase()

        self.sessions: dict[str, Session] = {}
        self.current_session_id: str = ""
        self._max_turns = 3
        self._lock = threading.Lock()

        self._load_session_state()

    # ------------------- Token 计算 -------------------------------

    @classmethod
    def _get_tokenizer(cls):
        if cls._tokenizer is None:
            cls._tokenizer = tiktoken.get_encoding("cl100k_base")
        return cls._tokenizer

    @staticmethod
    def _calculate_tokens(messages: list[dict]) -> int:
        encoder = RAGEngine._get_tokenizer()
        total = 0
        for msg in messages:
            for value in msg.values():
                if isinstance(value, str):
                    total += len(encoder.encode(value))
        return total

    # ------------------- 会话持久化 ------------------------------

    def _load_session_state(self) -> None:
        sp = Path(AppConfig.SESSION_FILE_PATH)
        if not sp.exists():
            print("[Session] 未找到存档，从零开始。")
            return
        try:
            d = json.loads(sp.read_text("utf-8"))
            if "sessions" in d:
                # 新格式：多会话
                for sid, sdata in d["sessions"].items():
                    self.sessions[sid] = Session(
                        session_id=sid,
                        title=sdata.get("title", "未命名对话"),
                        conversation_store=sdata.get("conversation_store", []),
                        summary_memory=sdata.get("summary_memory", ""),
                        created_at=sdata.get("created_at", ""),
                    )
                self.current_session_id = d.get("current_session_id", "")
                print(f"[Session] 恢复: {len(self.sessions)} 个会话")
            elif "conversation_store" in d:
                # 旧格式：迁移为单会话
                old_id = str(uuid.uuid4())
                old_session = Session(
                    session_id=old_id,
                    title="历史对话",
                    conversation_store=d.get("conversation_store", []),
                    summary_memory=d.get("summary_memory", ""),
                )
                self.sessions[old_id] = old_session
                self.current_session_id = old_id
                print("[Session] 旧格式已迁移为多会话")
        except Exception as e:
            print(f"[Session] 恢复失败: {e}")

    def _save_session_state(self) -> None:
        """保存所有会话状态到 JSON 文件。调用方必须持有 self._lock。"""
        data = {
            "current_session_id": self.current_session_id,
            "sessions": {
                sid: {
                    "title": s.title,
                    "conversation_store": s.conversation_store,
                    "summary_memory": s.summary_memory,
                    "created_at": s.created_at,
                }
                for sid, s in self.sessions.items()
            },
        }
        try:
            Path(AppConfig.SESSION_FILE_PATH).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except OSError as e:
            print(f"[Session] 保存失败: {e}")

    # ------------------- 多会话管理 ----------------------------

    def _current_session(self) -> Session:
        """获取当前会话对象（调用方应持有锁或在主线程中）。"""
        return self.sessions[self.current_session_id]

    def create_session(self) -> str:
        """创建新会话并返回 session_id。

        如果是第一个会话，自动设为当前会话。
        """
        sid = str(uuid.uuid4())
        session = Session(
            session_id=sid,
            title="新对话",
            conversation_store=[],
            summary_memory="",
        )
        with self._lock:
            self.sessions[sid] = session
            if not self.current_session_id:
                self.current_session_id = sid
                self._save_session_state()
        print(f"[Session] 创建: {sid[:8]}...")
        return sid

    def switch_session(self, session_id: str) -> bool:
        """切换到指定会话。成功返回 True。"""
        with self._lock:
            if session_id not in self.sessions:
                return False
            self.current_session_id = session_id
        print(f"[Session] 切换到: {session_id[:8]}...")
        return True

    def get_session_list(self) -> list[dict]:
        """返回所有会话的简要信息列表，按时间倒序。"""
        result: list[dict] = []
        for sid, s in self.sessions.items():
            result.append({
                "id": sid,
                "title": s.title,
                "message_count": len(s.conversation_store) // 2,
                "is_current": (sid == self.current_session_id),
            })
        result.sort(key=lambda x: x["id"], reverse=True)
        return result

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话。不允许删除最后一个会话。"""
        with self._lock:
            if session_id not in self.sessions:
                return False
            if len(self.sessions) <= 1:
                return False
            del self.sessions[session_id]
            if self.current_session_id == session_id:
                self.current_session_id = next(iter(self.sessions))
            self._save_session_state()
        # 清理该会话的向量化摘要（锁外网络请求）
        try:
            self.kb.clear_session_summaries(session_id)
        except Exception as e:
            print(f"[Session] 清理向量化摘要失败: {e}")
        print(f"[Session] 删除: {session_id[:8]}...")
        return True

    def rename_session(self, session_id: str, new_title: str) -> bool:
        """重命名指定会话。"""
        with self._lock:
            if session_id not in self.sessions:
                return False
            self.sessions[session_id].title = new_title
            self._save_session_state()
        print(f"[Session] 重命名: {session_id[:8]} -> '{new_title}'")
        return True

    # ------------------- 会话标题生成 ----------------------------

    def _generate_title(self, sid: str, question: str, answer: str) -> None:
        """异步生成对话标题（仅首轮调用）。"""

        def _run() -> None:
            try:
                title = self.llm.generate_response([{
                    "role": "user",
                    "content": TITLE_PROMPT.format(
                        question=question[:200], answer=answer[:200]),
                }])
                if title and not title.startswith("抱歉"):
                    title = title.strip()[:20]
                    with self._lock:
                        session = self.sessions.get(sid)
                        if session is not None:
                            session.title = title
                            self._save_session_state()
                    print(f"[Title] 生成: '{title}'")
            except Exception as e:
                print(f"[Title] 生成失败: {e}")

        threading.Thread(target=_run, daemon=True).start()

    # ------------------- L2 压缩逻辑 ------------------------------

    def _trigger_l2_compression(self) -> None:
        session = self._current_session()
        current_tokens = self._calculate_tokens(session.conversation_store)
        print(f"[Memory Check] 当前 L2 Token 数: {current_tokens} (阈值: {self.MAX_L2_TOKENS})")
        if current_tokens <= self.MAX_L2_TOKENS:
            print("[Memory Check] 未超阈值，无需压缩。")
            return

        popped: list[dict] = []
        while True:
            tokens = self._calculate_tokens(session.conversation_store)
            if tokens <= self.TARGET_L2_TOKENS or len(session.conversation_store) <= 6:
                break
            popped.append(session.conversation_store.pop(0))

        if popped:
            print(f"[L2 -> L3] 弹出 {len(popped)} 条旧消息, 启动异步摘要...")
            self._start_summary_worker(popped)
        else:
            print("[L2 -> L3] 异常: 未超阈值却进入压缩分支。")

    # ------------------- 异步 L3 摘要 -----------------------------

    def _start_summary_worker(self, old_messages: list[dict]) -> None:
        sid = self.current_session_id  # 捕获当前会话 ID，防止线程执行时会话已切换

        def _run() -> None:
            print("[L3 Worker] 异步摘要线程启动...")
            try:
                lines: list[str] = []
                for msg in old_messages:
                    role_label = "用户" if msg["role"] == "user" else "助手"
                    lines.append(f"{role_label}: {msg['content']}")
                summary = self.llm.generate_response([
                    {"role": "user", "content": SUMMARY_PROMPT.format(history_text="\n".join(lines))},
                ])
                if summary and not summary.startswith("抱歉"):
                    summary = summary.strip()
                    with self._lock:
                        session = self.sessions.get(sid)
                        if session is None:
                            return
                        if session.summary_memory:
                            session.summary_memory += "\n" + summary
                        else:
                            session.summary_memory = summary
                        print(f"[L3 Updated] 摘要: {session.summary_memory[:100]}...")
                        self._save_session_state()
                    # L3 向量化：将新摘要段存入向量库（锁外网络请求，可语义检索历史情节）
                    try:
                        self.kb.add_session_summary(sid, summary)
                    except Exception as e:
                        print(f"[L3 向量化] 摘要存储失败: {e}")

                with self._lock:
                    session = self.sessions.get(sid)
                    sm = session.summary_memory if session else ""
                if sm:
                    sm_tokens = self._calculate_tokens([{"role": "system", "content": sm}])
                    if sm_tokens > 4000:
                        print("[L3 压缩] L3 膨胀超限，触发二次压缩。")
                        compressed = self.llm.generate_response([
                            {"role": "user", "content": L3_COMPRESS_PROMPT.format(old_summaries=sm)},
                        ])
                        if compressed and not compressed.startswith("抱歉"):
                            compressed = compressed.strip()
                            with self._lock:
                                session = self.sessions.get(sid)
                                if session is not None:
                                    session.summary_memory = compressed
                                    print("[L3 压缩] 二次压缩完成")
                                    self._save_session_state()
                            # 压缩后的全局视角同样向量化存储
                            try:
                                self.kb.add_session_summary(sid, compressed, category="summary_compressed")
                            except Exception as e:
                                print(f"[L3 向量化] 压缩摘要存储失败: {e}")
            except Exception as e:
                print(f"[RAGEngine] L3 摘要失败: {e}")

        threading.Thread(target=_run, daemon=True).start()

    # ------------------- L4 实时维护 -------------------------

    def _start_memory_worker(self, current_q: str, current_a: str) -> None:
        sid = self.current_session_id  # 捕获当前会话 ID

        def _run() -> None:
            print("[L4 Worker] 实体记忆维护线程启动...")
            try:
                with self._lock:
                    session = self.sessions.get(sid)
                    if session is None:
                        return
                    recent = list(session.conversation_store[-6:])
                    sm = session.summary_memory or "(无)"

                recent_lines = []
                for msg in recent:
                    role_label = "用户" if msg["role"] == "user" else "助手"
                    recent_lines.append(f"{role_label}: {msg['content']}")
                recent_context = "\n".join(recent_lines) if recent_lines else "(无)"

                resp = self.llm.generate_response([{
                    "role": "user",
                    "content": L4_EXTRACT_PROMPT.format(
                        question=current_q, answer=current_a,
                        recent_context=recent_context, summary_memory=sm,
                    ),
                }])
                if not resp or resp.startswith("抱歉"):
                    return

                try:
                    candidates = json.loads(resp.strip())
                except json.JSONDecodeError:
                    s = resp.find("[")
                    e = resp.rfind("]") + 1
                    if s >= 0 and e > s:
                        try:
                            candidates = json.loads(resp[s:e])
                        except json.JSONDecodeError:
                            return
                    else:
                        return

                if not candidates:
                    return

                for item in candidates:
                    # 兼容新格式 {"fact": "...", "category": "..."} 与旧格式字符串
                    if isinstance(item, dict):
                        fact = (item.get("fact") or "").strip()
                        category = item.get("category") or "fact"
                    elif isinstance(item, str):
                        fact = item.strip()
                        category = "fact"
                    else:
                        continue
                    if not fact:
                        continue
                    if category not in ("fact", "preference", "episodic"):
                        category = "fact"
                    old_id, old_text = self.kb.search_l4_for_conflict(fact)
                    if old_id is None:
                        self.kb.add_l4_memory(fact, category)
                        print(f"[L4 Action] ADD: {fact} ({category})")
                    else:
                        decision_raw = self.llm.generate_response([{
                            "role": "user",
                            "content": L4_CONFLICT_PROMPT.format(old_memory=old_text, new_fact=fact),
                        }])
                        decision = decision_raw.strip().upper() if decision_raw else "NOOP"
                        if "UPDATE" in decision:
                            self.kb.update_l4_memory(old_id, fact)
                            print(f"[L4 Action] UPDATE: {old_text} -> {fact}")
                        elif "DELETE" in decision:
                            self.kb.delete_l4_memory(old_id)
                            print(f"[L4 Action] DELETE: {old_text}")
                        elif "ADD" in decision:
                            self.kb.add_l4_memory(fact, category)
                            print(f"[L4 Action] ADD (conflict): {fact} ({category})")
                        else:
                            print(f"[L4 Action] NOOP: {fact}")
            except Exception as e:
                print(f"[L4 Worker] 异常: {e}")

        threading.Thread(target=_run, daemon=True).start()

    # ------------------- 查询重写 ---------------------------------

    def _rewrite_query(self, question: str) -> str:
        session = self._current_session()
        if not session.conversation_store:
            return question
        recent = session.conversation_store[-(self._max_turns * 2):]
        rewritten = self.llm.generate_response(
            [{"role": "system", "content": REWRITE_SYSTEM_PROMPT}]
            + recent + [{"role": "user", "content": question}]
        )
        if rewritten.startswith("抱歉"):
            return question
        return rewritten.strip()

    # ------------------- 原生工具 Schema --------------------------

    @staticmethod
    def _get_local_tool_schemas() -> list[dict]:
        """返回 DeepSeek/OpenAI Function Calling 兼容的工具定义。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "获取指定城市某一天的天气情况（今天/明天/指定日期）。当用户询问天气、温度、是否能下雨、能否打药时调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "城市名称，如：阿拉尔、阿克苏",
                            },
                            "date": {
                                "type": "string",
                                "description": "要查询的日期，可以是 'today'（今天）、'tomorrow'（明天），或 YYYY-MM-DD 格式的具体日期（如 2025-06-15）。默认为 'today'。最多支持未来 3 天预报。",
                            },
                        },
                        "required": ["city"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "在互联网上搜索最新信息。当知识库中没有相关信息, 或用户询问最新政策、新闻时调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索关键词",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_cotton_stats",
                    "description": "查询/计算新疆棉花产量与播种面积统计数据。分地区数据覆盖 2015-2022 年，全区主要年份数据覆盖 1978-2022 年。支持：单点查询某地区某年产量/面积、亩产计算（公斤/亩）、多年合计/平均、某年产区排名、全区主要年份查询。当用户询问棉花产量、面积、亩产、产区排名等数据问题时调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "region": {
                                "type": "string",
                                "description": "地区名，如：阿克苏地区、塔城地区-沙湾市、生产建设兵团；全区汇总或主要年份查询用 '全区'。",
                            },
                            "year": {
                                "type": "integer",
                                "description": "年份，如 2020。分地区可查 2015-2022，主要年份可查 1978-2022。",
                            },
                            "metric": {
                                "type": "string",
                                "enum": ["yield", "area", "mu", "long_yield", "long_area", "long_mu"],
                                "description": "指标：yield=棉花产量、area=棉花播种面积、mu=棉花亩产（公斤/亩）、long_yield=长绒棉产量、long_area=长绒棉播种面积、long_mu=长绒棉亩产（公斤/亩）。",
                            },
                            "stat": {
                                "type": "string",
                                "enum": ["value", "total", "avg", "rank", "major"],
                                "description": "统计类型：value=单点查询（需 region+year）、total=多年合计、avg=多年平均、rank=某年产区排名（需 year）、major=全区主要年份（需 year）。",
                            },
                            "top": {
                                "type": "integer",
                                "description": "rank 时返回前 N 名，默认 5。",
                            },
                        },
                        "required": ["metric", "stat"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "calc_price_volatility",
                    "description": "查询/计算中国棉花价格指数（CC Index）数据。支持：单日/单月价格查询、波动率（日/年化）、区间涨跌幅、历史价格分位。六个等级：1129B/2129B/3128B/4128B/1228B/2227B，价格单位元/吨。数据范围 2016-01 ~ 2022-12。当用户询问棉花价格、价格波动、涨跌幅、价格历史位置等问题时调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "grade": {
                                "type": "string",
                                "enum": ["1129B", "2129B", "3128B", "4128B", "1228B", "2227B"],
                                "description": "价格等级，默认 3128B（CC Index 328 标准等级）。",
                            },
                            "metric": {
                                "type": "string",
                                "enum": ["price", "volatility", "pct_change", "percentile"],
                                "description": "操作：price=查询价格（需 date）、volatility=波动率（需 start/end，可加 period）、pct_change=区间涨跌幅（需 start/end）、percentile=历史价格分位（需 date）。",
                            },
                            "date": {
                                "type": "string",
                                "description": "价格/分位查询的日期，支持 YYYY-MM-DD（当日）或 YYYY-MM（当月最后一个交易日）。",
                            },
                            "start": {
                                "type": "string",
                                "description": "区间起始日期，YYYY-MM-DD 或 YYYY-MM。",
                            },
                            "end": {
                                "type": "string",
                                "description": "区间结束日期，YYYY-MM-DD 或 YYYY-MM（结束月份取当月最后交易日）。",
                            },
                            "period": {
                                "type": "string",
                                "enum": ["monthly", "annual"],
                                "description": "波动率类型：monthly=日波动率（日收益标准差）、annual=年化波动率（×√交易日数），默认 monthly。",
                            },
                        },
                        "required": ["metric"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "plot_trend",
                    "description": "绘制新疆棉花产量/面积/亩产/价格趋势折线图，支持多地区多系列对比。数据范围：分地区 2015-2022、全区主要年份 1978-2022、价格 2016-2022。当用户要求画图、图表、趋势、走势、对比图时调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "metric": {
                                "type": "string",
                                "enum": ["yield", "area", "mu", "long_yield", "long_area", "price"],
                                "description": "指标：yield=棉花产量、area=播种面积、mu=亩产（公斤/亩）、long_yield=长绒棉产量、long_area=长绒棉面积、price=价格指数（需 grade，按月取月末价）。",
                            },
                            "regions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "地区列表（产量/面积/亩产指标时用），如 [\"阿克苏地区\", \"喀什地区\"]；省略或空 = 全区。",
                            },
                            "start": {
                                "type": "string",
                                "description": "起始年份（如 2018）或起始月份（price 指标时，如 2021-01）。",
                            },
                            "end": {
                                "type": "string",
                                "description": "结束年份（如 2022）或结束月份（price 指标时，如 2022-12）。",
                            },
                            "grade": {
                                "type": "string",
                                "enum": ["1129B", "2129B", "3128B", "4128B", "1228B", "2227B"],
                                "description": "仅 price 指标使用，默认 3128B。",
                            },
                            "title": {
                                "type": "string",
                                "description": "可选，自定义图表标题。",
                            },
                        },
                        "required": ["metric"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "analyze_yield",
                    "description": "产量预测区间与产区波动风险分级。forecast：基于 2015-2022 历史数据线性趋势外推，预测任意未来年份产量/面积，返回点预测+置信区间+趋势+R2；risk：按变异系数（CV）对产区波动风险分级（低<0.15/中0.15-0.35/高≥0.35）。当用户问预测产量、未来产量、风险评估、波动风险时调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "metric": {
                                "type": "string",
                                "enum": ["yield", "area", "long_yield", "long_area"],
                                "description": "指标：yield=棉花产量、area=播种面积、long_yield=长绒棉产量、long_area=长绒棉面积。",
                            },
                            "stat": {
                                "type": "string",
                                "enum": ["forecast", "risk"],
                                "description": "操作：forecast=产量预测（需 region+target_year）、risk=风险分级（可加 regions 筛选）。",
                            },
                            "region": {
                                "type": "string",
                                "description": "forecast 时必填：要预测的地区名（必须与用户原话一致，如：阿克苏地区、喀什地区、生产建设兵团）。",
                            },
                            "target_year": {
                                "type": "integer",
                                "description": "forecast 时必填：预测目标年份（必须晚于 2022）。",
                            },
                            "confidence": {
                                "type": "number",
                                "description": "置信度，默认 0.90（可选 0.68/0.90/0.95，其他值线性插值）。",
                            },
                            "regions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "risk 时可选：仅分析指定地区；省略则分析全部产区。",
                            },
                        },
                        "required": ["metric", "stat"],
                    },
                },
            },
        ]

    # ------------------- 工具执行 ---------------------------------

    @staticmethod
    def _execute_tool(name: str, args: dict) -> str:
        """执行本地工具并返回文本结果。

        Args:
            name: 工具名称 (get_weather / web_search)。
            args: 工具参数。

        Returns:
            工具执行结果字符串; 失败时返回错误描述。
        """
        if name == "get_weather":
            city = args.get("city", "")
            if not city:
                return "天气查询失败: 未提供城市名。"
            date_str = args.get("date", "today")
            api_key = AppConfig.AMAP_API_KEY
            if not api_key:
                return "天气查询失败: 未配置高德地图 API Key。"

            # ── 计算目标日期相对于今天的偏移量 ──
            today = date.today()
            if date_str == "today":
                offset = 0
            elif date_str == "tomorrow":
                offset = 1
            else:
                try:
                    target = date.fromisoformat(date_str)
                    offset = (target - today).days
                except ValueError:
                    return f"天气查询失败: 日期格式错误 [{date_str}]，请使用 YYYY-MM-DD 格式。"

            if offset < 0:
                return f"天气查询失败: 无法查询过去的日期 [{date_str}]。"

            try:
                if offset == 0:
                    # 今天 → 实况天气 (extensions=base)
                    resp = requests.get(
                        "https://restapi.amap.com/v3/weather/weatherInfo",
                        params={"key": api_key, "city": city, "extensions": "base"},
                        timeout=10,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("status") != "1":
                        return f"天气查询失败: {data.get('info', '未知错误')}"
                    lives = data.get("lives", [])
                    if not lives:
                        return f"天气查询失败: 未找到城市 [{city}] 的天气数据。"
                    w = lives[0]
                    return (
                        f"城市: {w.get('city', city)}\n"
                        f"天气: {w.get('weather', '未知')}\n"
                        f"温度: {w.get('temperature', '未知')}°C\n"
                        f"风向: {w.get('winddirection', '未知')} "
                        f"风力: {w.get('windpower', '未知')}级\n"
                        f"湿度: {w.get('humidity', '未知')}%\n"
                        f"发布时间: {w.get('reporttime', '未知')}"
                    )
                else:
                    # 明天及以后 → 预报 (extensions=all), 最多 4 天
                    if offset > 3:
                        return (
                            f"天气查询失败: 仅支持查询今天起 4 天内的天气，"
                            f"{date_str} 超出预报范围（今天: {today}，最远可查: {today} 往后 3 天）。"
                        )
                    resp = requests.get(
                        "https://restapi.amap.com/v3/weather/weatherInfo",
                        params={"key": api_key, "city": city, "extensions": "all"},
                        timeout=10,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("status") != "1":
                        return f"天气查询失败: {data.get('info', '未知错误')}"
                    forecasts = data.get("forecasts", [])
                    if not forecasts:
                        return f"天气查询失败: 未找到城市 [{city}] 的预报数据。"
                    casts = forecasts[0].get("casts", [])
                    if offset >= len(casts):
                        return f"天气查询失败: 高德 API 未返回 {date_str} 的预报数据。"
                    t = casts[offset]
                    return (
                        f"城市: {forecasts[0].get('city', city)}\n"
                        f"日期: {t.get('date', date_str)}\n"
                        f"白天天气: {t.get('dayweather', '未知')}\n"
                        f"夜间天气: {t.get('nightweather', '未知')}\n"
                        f"白天温度: {t.get('daytemp', '未知')}°C\n"
                        f"夜间温度: {t.get('nighttemp', '未知')}°C\n"
                        f"白天风力: {t.get('daypower', '未知')}级\n"
                        f"夜间风力: {t.get('nightpower', '未知')}级"
                    )
            except requests.RequestException as e:
                return f"天气查询失败: 网络错误 - {e}"
            except (KeyError, IndexError, ValueError) as e:
                return f"天气查询失败: 数据解析错误 - {e}"
            except Exception as e:
                return f"天气查询失败: {e}"

        if name == "query_cotton_stats":
            return RAGEngine._execute_cotton_stats(args)

        if name == "calc_price_volatility":
            return RAGEngine._execute_price_volatility(args)

        if name == "plot_trend":
            return RAGEngine._execute_plot_trend(args)

        if name == "analyze_yield":
            return RAGEngine._execute_analyze_yield(args)

        if name == "web_search":
            query = args.get("query", "")
            if not query:
                return "Tavily 搜索失败"
            api_key = AppConfig.TAVILY_API_KEY
            if not api_key:
                return "错误：未配置 Tavily API Key"
            try:
                from tavily import TavilyClient
                client = TavilyClient(api_key=api_key)
                response = client.search(
                    query=query, max_results=3, search_depth="advanced",
                )
                results = response.get("results", [])
                if not results:
                    return "Tavily 搜索未返回有效结果。"
                lines: list[str] = []
                for r in results:
                    title = r.get("title", "")
                    content = r.get("content", "")
                    if not title and not content:
                        continue
                    if title:
                        lines.append(f"标题: {title}")
                    if content:
                        lines.append(f"摘要: {content}")
                    lines.append("---")
                if not lines:
                    return "Tavily 搜索未返回有效结果。"
                return "\n".join(lines)
            except Exception as e:
                print(f"[Tool Call] Tavily 错误: {e}")
                log.warning("Tavily 错误: %s", e)
                return f"Tavily 搜索失败，错误信息: {str(e)}。请告知用户搜索功能暂时不可用。"

        return f"工具未找到: {name}"

    @staticmethod
    def _execute_cotton_stats(args: dict) -> str:
        """执行棉花统计数据查询，返回 JSON 字符串供 LLM 组织语言。"""
        import json as _json

        metric = args.get("metric", "")
        stat = args.get("stat", "value")
        region = args.get("region", "")
        top = args.get("top", 5)

        # 非法 metric 安全校验（各查询函数已有校验，此处提前返回更友好的提示）
        valid_metrics = {"yield", "area", "mu", "long_yield", "long_area", "long_mu"}
        if metric not in valid_metrics:
            return _json.dumps(
                {"error": f"不支持的指标: {metric!r}，可选 {sorted(valid_metrics)}"},
                ensure_ascii=False,
            )

        year = args.get("year")
        if year is not None:
            try:
                year = int(year)
            except (TypeError, ValueError):
                return _json.dumps({"error": f"年份参数无效: {year}"}, ensure_ascii=False)

        if stat in ("total", "avg"):
            result = aggregate(region, metric, stat)
        elif stat == "rank":
            result = rank_by_year(year, metric, top)
        elif stat == "major":
            result = query_major(metric, year)
        else:  # value
            result = query_value(region, year, metric)

        return _json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _execute_price_volatility(args: dict) -> str:
        """执行棉花价格指数查询/计算，返回 JSON 字符串。"""
        import json as _json

        metric = args.get("metric", "")
        grade = args.get("grade", "3128B")
        date = args.get("date")
        start = args.get("start")
        end = args.get("end")
        period = args.get("period", "monthly")

        if metric == "price":
            if not date:
                return _json.dumps({"error": "price 查询需要 date 参数（YYYY-MM-DD 或 YYYY-MM）"},
                                   ensure_ascii=False)
            result = query_price(grade, date)
        elif metric == "volatility":
            result = calc_volatility(grade, start, end, period)
        elif metric == "pct_change":
            result = calc_pct_change(grade, start, end)
        elif metric == "percentile":
            if not date:
                return _json.dumps({"error": "percentile 查询需要 date 参数"},
                                   ensure_ascii=False)
            result = calc_percentile(grade, date)
        else:
            result = {"error": f"不支持的 metric: {metric}，可选 price/volatility/pct_change/percentile"}

        return _json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _execute_plot_trend(args: dict) -> str:
        """绘制趋势折线图：组装数据序列 → 生成 PNG → 返回路径与序列。

        返回 JSON: {"chart_path", "title", "series": [...], "note", "error"?}
        """
        import json as _json

        metric = args.get("metric", "")
        regions = args.get("regions") or []
        start = args.get("start")
        end = args.get("end")
        grade = args.get("grade", "3128B")
        title = (args.get("title") or "").strip()

        PRICE_METRICS = {"price"}
        YEARLY_METRICS = {"yield", "area", "mu", "long_yield", "long_area"}

        if metric not in PRICE_METRICS and metric not in YEARLY_METRICS:
            return _json.dumps(
                {"error": f"不支持的指标: {metric!r}，可选 "
                          "yield/area/mu/long_yield/long_area/price"},
                ensure_ascii=False,
            )

        try:
            if metric in PRICE_METRICS:
                # ── 价格月度序列（每等级一条线，regions 忽略） ──
                result = monthly_series(grade, start, end)
                if "error" in result:
                    return _json.dumps(result, ensure_ascii=False)
                series = [{
                    "label": f"{grade}（元/吨）",
                    "x": [p["month"] for p in result["points"]],
                    "y": [p["value"] for p in result["points"]],
                }]
                notes = []
                default_title = f"中国棉花价格指数 {grade} 月度走势"
                xlabel, ylabel = "月份", "价格（元/吨）"
            else:
                # ── 年度序列（每地区一条线） ──
                if not regions:
                    regions = ["全区"]
                series = []
                notes = []
                unit = ""
                for r in regions:
                    res = series_by_year(r, metric, int(start or 2015),
                                         int(end or 2022))
                    if "error" in res:
                        return _json.dumps({"error": f"地区「{r}」: {res['error']}",
                                            "candidates": res.get("candidates")},
                                           ensure_ascii=False)
                    if res.get("note"):
                        notes.append(res["note"])
                    unit = res.get("unit", unit)
                    series.append({
                        "label": res["region"],
                        "x": [str(p["year"]) for p in res["points"]],
                        "y": [p["value"] for p in res["points"]],
                    })
                metric_label = {
                    "yield": "棉花产量", "area": "棉花播种面积", "mu": "棉花亩产",
                    "long_yield": "长绒棉产量", "long_area": "长绒棉播种面积",
                }[metric]
                default_title = f"新疆{metric_label}趋势对比（{'、'.join(r for r in regions)}）"
                xlabel, ylabel = "年份", f"{metric_label}（{unit}）"
        except (TypeError, ValueError) as e:
            return _json.dumps({"error": f"参数解析失败: {e}"}, ensure_ascii=False)

        # ── 生成图片 ──
        try:
            out_path = next_chart_path("trend")
            generate_trend_chart(series, title or default_title,
                                 xlabel, ylabel, out_path)
        except Exception as e:
            return _json.dumps({"error": f"图表生成失败: {e}"}, ensure_ascii=False)

        resp: dict = {
            "chart_path": out_path.as_posix(),
            "title": title or default_title,
            "series": series,
        }
        if notes:
            resp["note"] = "；".join(notes)
        return _json.dumps(resp, ensure_ascii=False)

    @staticmethod
    def _execute_analyze_yield(args: dict) -> str:
        """执行产量预测/风险分级，返回 JSON 字符串。

        forecast 自动生成预测区间带图，risk 自动生成风险条形图，
        chart_path 随结果返回（复用现有 ![图表] 兜底嵌入机制）。
        """
        import json as _json

        metric = args.get("metric", "")
        stat = args.get("stat", "")

        METRIC_LABELS = {
            "yield": "棉花产量", "area": "棉花播种面积",
            "long_yield": "长绒棉产量", "long_area": "长绒棉播种面积",
        }

        try:
            if stat == "forecast":
                region = args.get("region", "")
                if not region:
                    return _json.dumps(
                        {"error": "forecast 需要 region 参数（必须使用用户原话中的地区名）"},
                        ensure_ascii=False,
                    )
                target_year = args.get("target_year")
                if target_year is None:
                    return _json.dumps(
                        {"error": "forecast 需要 target_year 参数（预测目标年份）"},
                        ensure_ascii=False,
                    )
                confidence = args.get("confidence", 0.90)
                result = forecast_yield(region, metric, target_year, confidence)
                if "error" not in result:
                    from core.trend_plot import generate_forecast_chart, next_chart_path
                    out = next_chart_path("forecast")
                    generate_forecast_chart(
                        result["region"], METRIC_LABELS.get(metric, metric),
                        result.get("unit", ""), result["history"],
                        result["target_year"], result["forecast"],
                        result["ci_lower"], result["ci_upper"], out,
                    )
                    result["chart_path"] = out.as_posix()
            elif stat == "risk":
                regions = args.get("regions")
                result = risk_ranking(metric, regions)
                if "error" not in result:
                    from core.trend_plot import generate_risk_chart, next_chart_path
                    out = next_chart_path("risk")
                    generate_risk_chart(
                        result["ranking"], METRIC_LABELS.get(metric, metric),
                        result.get("unit", ""), out,
                    )
                    result["chart_path"] = out.as_posix()
            else:
                result = {"error": f"不支持的 stat: {stat}，可选 forecast/risk"}
        except Exception as e:
            result = {"error": f"分析失败: {e}"}

        return _json.dumps(result, ensure_ascii=False)

    # ------------------- 组装 L1 消息 ----------------------------

    def _build_messages(self, question: str) -> list[dict]:
        search_query = self._rewrite_query(question)
        context = self.kb.retrieve_context(search_query, k=3)

        l4_facts = self.kb.retrieve_l4_memory(search_query, k=3)
        l4_section = ""
        if l4_facts:
            print(f"[L1 组装] L4 长期记忆命中: {len(l4_facts)} 条。")
            l4_section = "【用户长期记忆】:\n" + "\n".join(l4_facts) + "\n\n"

        session = self._current_session()
        with self._lock:
            sm = session.summary_memory

        summary_section = ""
        if sm:
            # L3 向量化后：语义召回相关历史摘要段（翻旧账）+ 当前完整摘要（最新状态）
            recalled: list[str] = []
            try:
                recalled = self.kb.retrieve_session_summaries(search_query, k=3)
            except Exception:
                recalled = []
            parts = []
            for seg in recalled:
                seg = seg.strip()
                if seg and seg != sm.strip() and seg not in parts:
                    parts.append(seg)
            if parts:
                summary_section = "【历史摘要】(来自长期记忆):\n" + "\n".join(parts) + "\n\n"
            summary_section += "【当前会话状态】:\n" + sm.strip() + "\n\n"

        system_content = SYSTEM_PROMPT.format(
            today_date=date.today(),
            summary_section=summary_section, l4_section=l4_section, context=context,
        )

        l3_label = "是" if sm else "否"
        l4_label = "是" if l4_facts else "否"
        l2_rounds = len(session.conversation_store) // 2
        print(f"[L1 组装] L3: {l3_label} | L4: {l4_label} | L2 轮数: {l2_rounds}")

        messages: list[dict] = [{"role": "system", "content": system_content}]
        messages.extend(session.conversation_store)
        messages.append({"role": "user", "content": question})
        return messages

    # ------------------- 工具调用循环 ----------------------------

    def _execute_tool_loop(
        self, messages: list[dict], tools: list[dict], model_name: str | None,
    ) -> list[dict]:
        """非流式工具调用循环: 反复请求 LLM 直到不再要求工具。返回更新后的 messages。"""
        max_loops = 3  # 安全上限
        for _ in range(max_loops):
            msg = self.llm.chat_with_tools(messages, tools, model_name)
            if msg is None:
                break

            # 如果没有工具调用，直接退出循环
            if not msg.tool_calls:
                break

            # 【关键修复】：必须先将包含 tool_calls 的 assistant 消息追加到历史中
            messages.append(msg)

            # 遍历并执行工具
            for tc in msg.tool_calls:
                func_name = tc.function.name
                try:
                    func_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    func_args = {}
                print(f"[Tool Call] 调用工具: {func_name}, 参数: {func_args}")
                log.info("调用工具: %s, 参数: %s", func_name, func_args)

                result = self._execute_tool(func_name, func_args)
                summary = result[:300] + ("..." if len(result) > 300 else "")
                print(f"[Tool Call] 结果: {summary}")
                log.info("工具结果: %s", summary)

                # 追加工具执行结果
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
        return messages

    # ------------------- 问答入口 (流式) --------------------------

    def ask(
        self, question: str,
        model_name: str | None = None,
        enable_thinking: bool = False,
    ) -> Generator[tuple[str, str], None, None]:
        """基于 RAG + 原生工具的增强问答 (流式输出).

        内部读写当前会话的 L2/L3, L4 和 RAG 检索全局共享。
        """
        self._trigger_l2_compression()

        messages = self._build_messages(question)

        # 工具调用循环
        tools = self._get_local_tool_schemas()
        messages = self._execute_tool_loop(messages, tools, model_name)

        # 流式最终回答
        full_answer = ""
        full_reasoning = ""
        for msg_type, chunk in self.llm.stream_response(
            messages, model_name=model_name, enable_thinking=enable_thinking,
        ):
            yield (msg_type, chunk)
            if msg_type == "content":
                full_answer += chunk
            elif msg_type == "reasoning":
                full_reasoning += chunk

        # 图表兜底：工具生成了图表但回复未嵌入 ![图表](path) 标记时，
        # 自动在回复末尾追加图片标记（GUI 渲染时提取为图片段）
        if full_answer and "![" not in full_answer:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "tool":
                    try:
                        d = json.loads(m.get("content", ""))
                        cp = d.get("chart_path")
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        cp = None
                    if cp:
                        img_mark = f"\n\n![图表]({cp})"
                        full_answer += img_mark
                        yield ("content", img_mark)
                        break

        # 更新 L2 并持久化
        session = self._current_session()
        is_first_turn = (len(session.conversation_store) == 0)

        with self._lock:
            session.conversation_store.append({"role": "user", "content": question})
            session.conversation_store.append(
                {"role": "assistant", "content": full_answer, "reasoning": full_reasoning}
            )
            self._save_session_state()

        # 首轮对话：异步生成对话标题
        if is_first_turn and full_answer:
            sid = self.current_session_id
            self._generate_title(sid, question, full_answer)

        # 异步 L4 实体记忆维护
        self._start_memory_worker(question, full_answer)
