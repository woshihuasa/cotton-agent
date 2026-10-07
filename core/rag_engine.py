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

import tiktoken

log = logging.getLogger("rag")

from config import AppConfig
from core.knowledge_base import KnowledgeBase
from core.llm_client import LLMClient
from core.tools import TOOL_SCHEMAS, execute_tool
from core.user_memory import NullMemoryStore, UserMemoryStore


# ---------------------------------------------------------------------------
# 工具结果 JSON 序列化
# ---------------------------------------------------------------------------


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
    "【回答质量约束】(以下四条对所有回答生效, 优先级高于上面的工具规则):\n"
    "1. 数据一致性: 回答中引用的任何数字都必须与工具返回或【背景知识】严格一致, "
    "不得改写口径、不得改变量级、不得前后矛盾. "
    "若同一回答中引用了多处数据, 务必自行核对彼此不冲突; "
    "确实无法调和的冲突, 就不要同时呈现, 并说明数据口径差异.\n"
    "2. 完整性: 回答前先逐项确认问题里的**每一项要求**都已回应. "
    "凡问题中出现「给建议」「怎么办」「如何」「有什么风险」等字样, "
    "除数据与结论外**必须给出对应的建议段落**, 不得只给数据就结束. "
    "同时注意不要被截断: 宁可精简中间展开, 也要保证结尾完整.\n"
    "3. 合规边界(重要), 分两类处理, **不要混为一谈**:\n"
    "   (a) 询问**行情位置与决策倾向**——如「现在价格算高吗」「该不该囤货」"
    "「什么时候卖合适」: 可以回答, 但**只陈述客观事实与风险因素**"
    "(如历史分位、波动率、供需与政策情况), "
    "**不得给出买卖建议、不得预测涨跌、不得建议仓位**, "
    "结尾说明「以上为客观数据与风险因素, 不构成投资建议」.\n"
    "   (b) 询问**具体交易操作**——如「套期保值具体怎么操作」「保证金比例多少」"
    "「怎么开户」「多少手」「买卖点位」: 这**超出本助手的能力范围**"
    "(本助手面向棉花种植, 不具备期货交易与投资顾问资质), "
    "应**明确说明无法提供此类操作指导**, 可建议用户咨询具备资质的期货机构, "
    "并回到种植相关的可答话题. **不得**因为联网搜到了资料就代为给出操作步骤.\n"
    "4. 能力边界: 与新疆棉花种植无关的问题(如加密货币、股票、其他作物等), "
    "应说明本助手只覆盖新疆棉花种植与产业信息, 无法回答, 不要勉强作答.\n"
    "5. 收尾: 所有工具调用完成后, 必须输出一段完整的最终文字回答; "
    "不得以工具调用记录、原始 JSON 或纯数据罗列作为回答的结尾.\n\n"
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
    '[{{"fact": "用户种植新陆早77号", "category": "fact"}}, '
    '{{"fact": "用户偏好早熟品种", "category": "preference"}}]\n'
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
    "下面是【候选旧记忆】(按相关度排序) 和一条【新候选事实】。\n"
    "请判断应该执行什么操作。\n\n"
    "**重要: 候选是按语义相似度排序的, 语义相近不代表是同一个属性。**\n"
    "请逐条判断「讲的是不是同一个属性槽位」, 不要因为措辞像就认为是同一条。\n\n"
    "判断规则 (按顺序判断, 命中即返回):\n"
    "- NOOP: 某条候选与新事实讲的是**同一件事**, 只是措辞 / 详略 / 语序不同。\n"
    '    · 近义改写: "用户配种长绒棉" 与 "用户配置种植长绒棉"\n'
    '    · 加限定词但信息未变: "用户10月中下旬机采" 与 "用户长期目标是10月中下旬机采"\n'
    '    · 计划与陈述互转: "用户计划8月31日停水" 与 "用户8月31日停水"\n'
    "  **关键: 更详细不等于新信息。**\n"
    "- UPDATE n: 第 n 条候选与新事实是**同一个属性**, 但取值变了。\n"
    '    · 品种更换: "主栽品种是塔河2号" 与 "已改种新陆早77号"  → 同一属性(品种)\n'
    '    · 时间变更: "计划8月底停水" 与 "停水时间改为9月10日"    → 同一属性(停水时间)\n'
    '    · 计划取消: "计划10月售棉" 与 "取消了售棉计划"          → 同一属性(售棉计划)\n'
    "- DELETE n: 第 n 条候选被明确否定, 且新事实未给出替代取值。\n"
    "- ADD: **所有候选**讲的都是不同属性或**不同对象**, 新事实应作为补充新增。\n"
    '    · **不同对象必须判 ADD**: "在阿克苏有100亩滴灌棉田" 与 "在阿拉尔有20亩滴灌棉田"\n'
    "      是两块不同的地, 绝不能 UPDATE —— 那会抹掉其中一条。\n\n"
    "输出格式 (只输出一行, 不要解释):\n"
    "    ADD          —— 新增\n"
    "    NOOP         —— 无需改动\n"
    "    UPDATE n     —— 用新事实替换第 n 条候选\n"
    "    DELETE n     —— 删除第 n 条候选\n\n"
    "【候选旧记忆】\n{candidates}\n\n"
    "【新候选事实】\n{new_fact}"
)


def _parse_l4_decision(raw: str | None) -> tuple[str, int | None]:
    """从 LLM 回复中解析冲突消解动作 → (action, candidate_index)。

    只认**首个词**（并剥离标点），避免把解释性文字里的关键词误当动作——
    例如 "应 NOOP, 不应 ADD" 用旧的子串匹配会命中 ADD 而判反。

    Args:
        raw: LLM 原始回复。

    Returns:
        (action, index)。action ∈ {ADD, UPDATE, DELETE, NOOP}；
        index 为 **1-based** 候选序号，仅 UPDATE / DELETE 有意义，未给出时为 None。

    解析失败一律回退 ("NOOP", None)：当前主要风险是记忆**过度膨胀**（短板 #19）
    与**误写销毁事实**（短板 #23），因此"不写"比"误写"更保守。
    """
    if not raw:
        return "NOOP", None
    parts = raw.strip().split()
    if not parts:
        return "NOOP", None

    token = parts[0].strip(".,:;!?*`\"'()[]{}，。：；！？、（）【】").upper()
    if token not in ("ADD", "UPDATE", "DELETE", "NOOP"):
        return "NOOP", None

    index: int | None = None
    if token in ("UPDATE", "DELETE"):
        # 容忍 "UPDATE 2" / "UPDATE #2" / "UPDATE 第2条" / "UPDATE 2." 等写法
        for p in parts[1:4]:
            digits = "".join(ch for ch in p if ch.isdigit())
            if digits:
                index = int(digits)
                break
    return token, index


# 每次冲突消解交给 LLM 的候选条数。取 5 是经验值：既能让"同属性但取值变了"
# 的低相似度候选（余弦 0.4~0.6）进入判定，又不至于把无关事实灌进 prompt。
_L4_CANDIDATE_K = 5


# 「不同对象」判定用的地区/地块标识。
# 用途：在线 UPDATE/DELETE 前的**要素闸**——地区标识互斥时拒绝覆盖。
# 为什么不能改用「数字闸」：品种名里就带数字（塔河2号 → 新陆早77号），
# 数字集互不为子集，会把**真正该 UPDATE** 的品种变更误拦。
_L4_REGION_TOKENS = (
    "阿克苏", "阿拉尔", "喀什", "库尔勒", "石河子", "沙湾", "精河", "阿瓦提",
    "和田", "吐鲁番", "哈密", "昌吉", "博尔塔拉", "塔城", "伊犁", "巴音郭楞",
    "克孜勒苏", "乌鲁木齐", "克拉玛依", "图木舒克", "五家渠", "北屯", "铁门关",
    "双河", "可克达拉", "昆玉", "胡杨河", "新星", "南疆", "北疆", "东疆",
)


def _different_object(a: str, b: str) -> bool:
    """两条事实是否明确指向**不同地区/地块**（要素闸）。

    E4 实测到：「用户在阿克苏有100亩滴灌棉田」被原地 UPDATE 成
    「用户在阿拉尔有20亩滴灌棉田」—— 静默销毁一条真实事实（短板 #23）。
    本闸门在写入前拦下这类覆盖，降级为 ADD。

    只在**双方都出现地区标识且完全不重叠**时判为不同对象；
    一方无地区标识（或两者同地区、只是取值变了）则不拦，放行 UPDATE。
    """
    ra = {t for t in _L4_REGION_TOKENS if t in a}
    rb = {t for t in _L4_REGION_TOKENS if t in b}
    return bool(ra and rb and ra.isdisjoint(rb))

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

    def __init__(
        self,
        api_key: str | None = None,
        user_data_dir: str | Path | None = None,
        owns_user_data: bool = True,
    ) -> None:
        """初始化引擎。

        Args:
            api_key: 可选的**实例级** LLM API Key（Web 服务"用户自备 Key"场景）。
                为 None 时使用全局配置，桌面端行为不变。
            user_data_dir: **用户数据作用域**。None = 生产路径（桌面端，行为不变）；
                传入目录则会话存档与记忆库都落在该目录下——用于评测沙箱或
                将来的多用户隔离。
            owns_user_data: 本引擎是否**拥有**该用户的数据。桌面端 True（默认）；
                Web 服务多访客场景传 False ——此时会话不落盘、L4 使用
                `NullMemoryStore`，既不读写生产库也不产生额外外泄面。
                详见 ROADMAP「用户数据隔离 S2」。
        """
        self.llm = LLMClient(api_key=api_key)
        self.kb = KnowledgeBase()        # 公共知识库（只读、可重建、可共享）

        # ---- 用户数据作用域 ----
        _scope = Path(user_data_dir) if user_data_dir else None
        self._owns_user_data = bool(owns_user_data)
        self._user_data_dir = _scope
        self._session_file = (
            (_scope / "session_state.json") if _scope
            else Path(AppConfig.SESSION_FILE_PATH)
        )

        if not self._owns_user_data:
            self.memory = NullMemoryStore()      # 不记忆（Web）
        else:
            _mem_path = str(_scope / "chroma_user_memory") if _scope else None
            self.memory = UserMemoryStore(db_path=_mem_path)

        self.sessions: dict[str, Session] = {}
        self.current_session_id: str = ""
        self._max_turns = 3
        self._lock = threading.Lock()

        self._load_session_state()

    @property
    def owns_user_data(self) -> bool:
        """本引擎是否拥有用户数据（供隔离验证与日志使用）。"""
        return self._owns_user_data

    @property
    def session_file(self) -> Path:
        """当前会话存档路径（供隔离验证使用）。"""
        return self._session_file

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
        if not self._owns_user_data:
            # Web 模式：会话仅存在于内存，不读取也不写入任何存档
            print("[Session] 本引擎不拥有用户数据 —— 跳过会话存档恢复。")
            return
        sp = self._session_file
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
        if not self._owns_user_data:
            return          # Web 模式：会话仅存在于内存
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
            self._session_file.write_text(
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
            self.memory.clear_session_summaries(session_id)
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
            # 成对弹出（user + assistant）：保证剩余历史以 user 消息开头。
            # 若逐条弹出，剩余序列可能以 assistant 开头——部分 API 实现会因此判参数非法（400）
            if len(session.conversation_store) >= 2:
                popped.append(session.conversation_store.pop(0))
                popped.append(session.conversation_store.pop(0))
            else:
                break

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
                        self.memory.add_session_summary(sid, summary)
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
                                self.memory.add_session_summary(sid, compressed, category="summary_compressed")
                            except Exception as e:
                                print(f"[L3 向量化] 压缩摘要存储失败: {e}")
            except Exception as e:
                print(f"[RAGEngine] L3 摘要失败: {e}")

        threading.Thread(target=_run, daemon=True).start()

    # ------------------- L4 实时维护 -------------------------

    def resolve_memory_fact(self, fact: str, category: str = "fact") -> str:
        """对**单条**候选事实执行冲突消解并落库，返回实际执行的动作。

        这是 L4 写入路径的**唯一实现**——`_start_memory_worker` 与
        `tests/eval_memory.py`（E4 记忆评测集）都调用它，
        确保评测验证的是生产代码本身，而不是一份复制的逻辑。

        【流程】候选检索(top-K, 无硬阈值) → LLM 判定 → 越界防护 → 要素闸 → 落库

        为什么是 **top-K + 无硬阈值**：旧实现用 `1/(1+L2²) >= 0.7`（等价余弦 0.786）
        当闸门，导致"同属性但取值变了"的情形（品种更换 0.51 / 计划取消 0.51 /
        放弃扩种 0.42）**永远进不了判定**，直接新增——这是记忆膨胀的结构性主因
        （ROADMAP 短板 #22）。相似度现降级为**排序信号**。

        Returns:
            "ADD" / "UPDATE" / "DELETE" / "NOOP"
        """
        candidates = self.memory.search_l4_candidates(fact, k=_L4_CANDIDATE_K)
        if not candidates:
            # 库中无任何记忆（冷启动）——无物可比，直接新增且不消耗 LLM 调用
            self.memory.add_l4_memory(fact, category)
            print(f"[L4 Action] ADD: {fact} ({category})")
            return "ADD"

        listing = "\n".join(f"{i + 1}. {text}" for i, (_, text, _) in enumerate(candidates))
        decision_raw = self.llm.generate_response([{
            "role": "user",
            "content": L4_CONFLICT_PROMPT.format(candidates=listing, new_fact=fact),
        }])
        action, index = _parse_l4_decision(decision_raw)

        if action in ("UPDATE", "DELETE"):
            target = self._pick_l4_candidate(candidates, index)
            if target is None:
                # 越界或未给序号且候选多于一条 → 保守放弃（宁可陈旧，不可写错）
                print(f"[L4 Action] NOOP (候选序号无效: {decision_raw!r}): {fact}")
                return "NOOP"
            tgt_id, tgt_text = target

            # 要素闸：对象不同则拒绝覆盖（防「阿克苏100亩」被「阿拉尔20亩」抹掉）
            if _different_object(tgt_text, fact):
                self.memory.add_l4_memory(fact, category)
                print(f"[L4 Action] ADD (要素闸·对象不同): {fact} ({category})")
                return "ADD"

            if action == "DELETE":
                self.memory.delete_l4_memory(tgt_id)
                print(f"[L4 Action] DELETE: {tgt_text}")
            else:
                self.memory.update_l4_memory(tgt_id, fact)
                print(f"[L4 Action] UPDATE: {tgt_text} -> {fact}")
            return action

        if action == "ADD":
            self.memory.add_l4_memory(fact, category)
            print(f"[L4 Action] ADD (conflict): {fact} ({category})")
            return "ADD"

        print(f"[L4 Action] NOOP: {fact}")
        return "NOOP"

    @staticmethod
    def _pick_l4_candidate(
        candidates: list[tuple[str, str, float]], index: int | None,
    ) -> tuple[str, str] | None:
        """按 LLM 给出的 1-based 序号取候选；序号缺失或越界时返回 None。

        缺失时**仅当候选唯一**才接受（此时无歧义）；多条候选却未指定序号 → 放弃，
        而不是猜一个——错 UPDATE 会销毁事实（短板 #23）。
        """
        if index is None:
            return (candidates[0][0], candidates[0][1]) if len(candidates) == 1 else None
        if 1 <= index <= len(candidates):
            c = candidates[index - 1]
            return c[0], c[1]
        return None

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
                    self.resolve_memory_fact(fact, category)
            except Exception as e:
                import traceback
                print(f"[L4 Worker] 异常: {e}")
                traceback.print_exc()

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
        """返回工具定义（OpenAI / DeepSeek Function Calling 兼容）。

        **实现已迁至 `core.tools.schemas`**，此处保留为向后兼容的委托入口，
        使 `tests/eval_tools.py` 等既有调用方无需改动。
        """
        return TOOL_SCHEMAS

    # ------------------- 工具执行 ---------------------------------

    @staticmethod
    def _execute_tool(name: str, args: dict) -> str:
        """执行工具并返回文本结果。

        **实现已迁至 `core.tools`**，此处保留为向后兼容的委托入口。
        桌面端（本类）与 MCP 服务端共用 `core.tools` 的同一份实现与契约，
        因此不存在"两套工具逻辑各自演化"的风险（ROADMAP 方向 D1）。
        """
        return execute_tool(name, args)


    # ------------------- 组装 L1 消息 ----------------------------

    def _build_messages(self, question: str) -> list[dict]:
        # 截断保护：Embedding 模型（bge-large-zh）最大输入约 512 token，
        # 过长的查询/改写结果会导致 Embedding API 返回 400（code 20015）使整条链路失败
        search_query = self._rewrite_query(question)[:400]
        # 降级保护：检索失败（网络/API/维度等异常）不应导致整个问答失败，退化为无上下文回答
        try:
            context = self.kb.retrieve_context(search_query, k=3)
        except Exception as e:
            print(f"[L1 组装] 知识库检索失败（降级为空上下文）: {e}")
            context = ""

        try:
            l4_facts = self.memory.retrieve_l4_memory(search_query, k=3)
        except Exception as e:
            print(f"[L1 组装] L4 记忆检索失败（降级为空）: {e}")
            l4_facts = []
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
                recalled = self.memory.retrieve_session_summaries(search_query, k=3)
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

        # 只透传 API 认识的标准字段——防御性过滤：
        # conversation_store 的 assistant 消息还携带 "reasoning" 等扩展字段（供 UI 回显），
        # 原样发送给上游 API 可能触发 400 参数错误（严格校验的实现会拒绝未知字段）
        _STD_FIELDS = ("role", "content", "name", "tool_calls", "tool_call_id")
        messages: list[dict] = [{"role": "system", "content": system_content}]
        messages.extend(
            {k: v for k, v in m.items() if k in _STD_FIELDS}
            for m in session.conversation_store
        )
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
