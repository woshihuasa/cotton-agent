"""
RAG 引擎模块 (v5 - 多会话管理 + 四层记忆)

整合大模型 (LLMClient)、本地知识库 (KnowledgeBase)、原生工具,
实现检索增强生成 (RAG) 问答流程, 支持联网搜索与天气查询。

工具:
  web_search  — Tavily 搜索 API (tavily-python)
  get_weather — 高德地图天气 API (requests)

记忆架构:
  L1  实时上下文 -> System Prompt 注入 L3 摘要 + L4 实体记忆 + 技能库经验提示 + 背景知识
  L2  近期对话   -> 每会话独立 conversation_store (Token 超阈值压缩)
  L3  长期摘要   -> 每会话独立 summary_memory (异步线程生成)
  L4  实体记忆   -> ChromaDB user_memory (全局跨会话共享)
  技能结晶档     -> procedural_memory/index.jsonl (L4 容器第三库; 零 LLM; 不含用户数据; **本机私有**)
  手写技能       -> skills/ + external_skill_dirs (**只读挂载**; 真相源在 git; 随包分发)

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

from config import AppConfig, skill_mount_dirs
from core.knowledge_base import KnowledgeBase
from core.procedural_memory import INDEX_NAME, RUNTIME_SUBDIR, ProceduralMemoryStore
from core import (context_budget, handoff, prompt_layers, providers,
                   query_router)
from core.llm_client import DEFAULT_REASONING_EFFORT
from core.skill_loader import SkillLibrary
from core.tools import TOOL_SCHEMAS, capabilities as tool_caps, execute_tool
from core.tools import dispatch
from core import trajectory
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
    "不得以工具调用记录、原始 JSON 或纯数据罗列作为回答的结尾.\n"
    "6. **时效性(重要)**: 【背景知识】的每段前面都标了〔出处 | 年份〕. "
    "知识库里同一主题存有**多个年份版本**(如灌溉指导意见有 2023/2024/2025/2026), "
    "且内容逐年高度重复. 因此:\n"
    "   (a) 引用背景知识时,**必须写明依据的年份**; "
    "若同一段同时用到多篇, 取**年份最新**的那篇作为主依据;\n"
    "   (b) 若标记为「年份：未标注」, **不要假设它是最新的** —— "
    "这类文档未标年份, 应说明\"该资料未标注年份\", 并提示用户以当地农技部门意见为准;\n"
    "   (c) 若不同年份的资料对同一做法给出**不同参数**(如滴水量、用药量), "
    "**应指出存在版本差异并说明各自年份**, 不要把它们混成一个结论.\n\n"
    "{summary_section}"
    "{l4_section}"
    "{skill_section}"
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
    "【关于候选末尾括号里的日期】\n"
    "那是该条**被记录的时间**。它只是**参考信息, 不是判据**:\n"
    "- **计划不得据时间覆盖事实**: 「用户有100亩棉田」(旧·事实) 与 "
    "「用户计划扩种到200亩」(新·计划) —— 后者只是计划, 前者**仍是当前事实**, "
    "不得因为更新就 UPDATE 掉前者。\n"
    "- 反之, 若新事实是**同一属性的取值更新**(品种更换 / 停水时间调整 / 计划取消), "
    "时间较新可以作为佐证。\n"
    "- 日期缺失或明显不合理时**完全忽略它**, 按上面的规则正常判断。\n\n"
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


def _first_system_prompt(messages: list[dict]) -> str:
    """取组装好的第一条 system 消息（轨迹导出要用它还原上下文）。

    ★ 只取 system，**不取检索片段** —— 片段会随知识库变化，写进轨迹会让
    同一份数据在不同时间导出得到不同内容（无法复现）。
    """
    for m in messages or ():
        if isinstance(m, dict) and m.get("role") == "system":
            return str(m.get("content") or "")
    return ""


def _extract_reasoning(msg) -> str:
    """从 chat completion 消息里取推理内容。

    ★ 三家供应商的位置不同：属性、model_extra、或压根没有 ——
    统一在这里取，**取不到就返回空串**（不要因此影响主流程）。
    """
    for attr in ("reasoning_content", "reasoning"):
        v = getattr(msg, attr, None)
        if isinstance(v, str) and v:
            return v
    extra = getattr(msg, "model_extra", None)
    if isinstance(extra, dict):
        for k in ("reasoning_content", "reasoning"):
            v = extra.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


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
        # 默认客户端走 `providers.default_client()` —— 它是"换服务商"的唯一入口：
        # · `LLM_PROVIDER` 留空时**完全等价于原来的 `LLMClient(api_key=api_key)`**
        #   （读 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL），行为与改动前逐字一致；
        # · 设为 `qwen` 等已登记名时，端点与密钥取自登记表 + 凭据层。
        self.llm = providers.default_client(api_key=api_key)
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

        # ---- 技能结晶档（L4 长期记忆容器第三库）----
        # 与画像库刻意分开：条目只含"工具链 + 参数名"，**不含任何用户数据**。
        # 落在**运行区**（本机私有）；随包分发的是**手写技能** `skills/`，
        # 两者经只读挂载点分开（docs/ARCHITECTURE.md §3–§4）。
        # 零第三方依赖 → 不受 chromadb / embedding 故障影响，构造开销可忽略。
        _skill_path = (_scope / RUNTIME_SUBDIR / INDEX_NAME) if _scope else None
        self.crystallized = ProceduralMemoryStore(
            path=_skill_path, mount_dirs=skill_mount_dirs(),
        )

        # ---- 手写技能（只读挂载点：随版本分发的 skills/ + 外部挂载）----
        # 加载机制是**提示注入**：解析 frontmatter → 按触发词匹配 → 渲染成文本。
        # **不注册工具、不执行脚本、不建索引**（docs/ARCHITECTURE.md §8）。
        self.skill_lib = SkillLibrary(mount_dirs=skill_mount_dirs())

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
        candidates = self.memory.search_l4_candidates(
            fact, k=_L4_CANDIDATE_K, with_time=True,
        )
        if not candidates:
            # 库中无任何记忆（冷启动）——无物可比，直接新增且不消耗 LLM 调用
            self.memory.add_l4_memory(fact, category)
            print(f"[L4 Action] ADD: {fact} ({category})")
            return "ADD"

        # 候选带上「记录时间」(asserted_at) 作为**冲突消解的参考信号** ——
# 只取日期部分：精确到秒对判断无益，反而增加噪声。缺时间时不留空括号。
        listing = "\n".join(
            f"{i + 1}. {text}" + (f"（{ts[:10]}）" if ts else "")
            for i, (_id, text, _cos, ts) in enumerate(candidates)
        )
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
        candidates: list[tuple],        # (id, text, cos[, asserted_at]) —— 只按下标取值，故兼容两种长度
        index: int | None,
    ) -> tuple[str, str] | None:
        """按 LLM 给出的 1-based 序号取候选；序号缺失或越界时返回 None。

        缺失时**仅当候选唯一**才接受（此时无歧义）；多条候选却未指定序号 → 放弃，
        而不是猜一个——错 UPDATE 会销毁事实（短板 #23）。

        ⚠️ **`index` 是必需形参**。曾为支持 4 元组（带 `asserted_at`）改写类型注解时，
        把 `index: int | None` 一起删掉了 —— 函数体与调用点仍在用它，于是每次 L4 维护都抛
        `TypeError: takes 1 positional argument but 2 were given`，**记忆写入全线静默失效**
        （异常被 worker 的 try/except 吞掉，只在日志里留痕）。守卫无法覆盖线程内异常，
        所以这条注释是为了让"删之前先想清楚"这件事有迹可循。
        """
        if index is None:
            return (candidates[0][0], candidates[0][1]) if len(candidates) == 1 else None
        if 1 <= index <= len(candidates):
            c = candidates[index - 1]
            return c[0], c[1]
        return None

    def _start_memory_worker(self, current_q: str, current_a: str) -> None:
        # 不拥有用户数据（Web 多访客）→ **根本不启动 worker**，而不是"启动了但不落库"。
        # 因为 worker 第一件事就是发起一次**抽取用 LLM 调用**（见下方 generate_response），
        # 而 Web 的 LLM Key 由访客自备 —— 白花访客的钱与延迟。
        # 这正是 NullMemoryStore docstring 的承诺，此前只在存储层实现、LLM 层未实现。
        if not self._owns_user_data:
            return
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

    def _build_messages(self, question: str, route=None) -> list[dict]:
        # 分级网关（零 LLM）：**默认 L2**，只有明确命中白名单才降级。
        # 取值保守——误降级（该检索却直答）用户无从察觉，省下的钱不该以答错为代价。
        decision = route if route is not None else query_router.classify(question)
        print(f"[L1 路由] {query_router.describe(decision)}")

        # 截断保护：Embedding 模型（bge-large-zh）最大输入约 512 token，
        # 过长的查询/改写结果会导致 Embedding API 返回 400（code 20015）使整条链路失败
        search_query = self._rewrite_query(question)[:400]
        # 降级保护：检索失败（网络/API/维度等异常）不应导致整个问答失败，退化为无上下文回答
        context = ""
        if decision.skip_retrieval:
            print("[L1 组装] L0/L1 已跳过知识库检索（省一次 embedding + 一次 rerank）")
        else:
            try:
                context = self.kb.retrieve_context(search_query, k=3)
            except Exception as e:
                print(f"[L1 组装] 知识库检索失败（降级为空上下文）: {e}")
                context = ""

        l4_facts = []
        if decision.skip_memory:
            print("[L1 组装] L0 已跳过 L4 记忆召回（省一次 embedding）")
        else:
            try:
                l4_facts = self.memory.retrieve_l4_memory(search_query, k=3)
            except Exception as e:
                print(f"[L1 组装] L4 记忆检索失败（降级为空）: {e}")
                l4_facts = []
        l4_section = ""
        if l4_facts:
            print(f"[L1 组装] L4 长期记忆命中: {len(l4_facts)} 条。")
            l4_section = "【用户长期记忆】:\n" + "\n".join(l4_facts) + "\n\n"

        # 技能：刻意用**原始问句**匹配（不走 search_query），因此本层零 LLM 开销。
        # 两个来源共用一个 prompt 槽，顺序固定为「结晶档在前、人写技能在后」：
        #   · 结晶档 = 自下而上（引擎观察到的轨迹，implicit）
        #   · 技能   = 自上而下（人写的编排，explicit）—— docs/ARCHITECTURE.md §8
        crys_label = skill_label = "否"
        skill_section = ""
        if not decision.skip_memory:
            try:
                crystal = self.crystallized.format_for_question(question)
                if crystal:
                    skill_section += crystal
                    crys_label = "是"
            except Exception as e:
                print(f"[L1 组装] 结晶档检索失败（降级为空）: {e}")
            try:
                authored = self.skill_lib.format_for_question(question)
                if authored:
                    skill_section += authored
                    skill_label = "是"
            except Exception as e:
                print(f"[L1 组装] 手写技能加载失败（降级为空）: {e}")
        else:
            print("[L1 组装] L0 已跳过技能注入")

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

        # ---- 上下文双层预算（先逐源硬上限，再全局代价最小化）----
        # 审计行让「prompt 为什么这么长」从猜测变成可复盘的事实。
        # 失败时**降级为不裁剪**（宁可长，不可因预算层出错而丢上下文）。
        try:
            _pri = context_budget.SOURCE_PRIORITY
            _budget = context_budget.fit([
                context_budget.SourceBlock("l3_summary", summary_section, _pri["l3_summary"]),
                context_budget.SourceBlock("l4_memory", l4_section, _pri["l4_memory"]),
                context_budget.SourceBlock("skill_slot", skill_section, _pri["skill_slot"]),
                context_budget.SourceBlock("kb_context", context, _pri["kb_context"]),
            ])
            summary_section = _budget.text_of("l3_summary")
            l4_section = _budget.text_of("l4_memory")
            skill_section = _budget.text_of("skill_slot")
            context = _budget.text_of("kb_context")
            print(f"[L1 预算] {_budget.describe()}")
        except Exception as e:
            print(f"[L1 预算] 预算层失败（降级为不裁剪）: {e}")

        system_content = SYSTEM_PROMPT.format(
            today_date=date.today(),
            summary_section=summary_section, l4_section=l4_section,
            skill_section=skill_section, context=context,
        )

        l3_label = "是" if sm else "否"
        l4_label = "是" if l4_facts else "否"
        l2_rounds = len(session.conversation_store) // 2
        print(f"[L1 组装] L3: {l3_label} | L4: {l4_label} | "
              f"结晶: {crys_label} | 技能: {skill_label} | L2 轮数: {l2_rounds}")

        # 提示分层审计 + 稳定前缀指纹（零副作用：只读模板、只算哈希，不改拼装）
        # stable_hash 进日志后，同一日期内的命中率可归因；一旦有人把可变内容插进
        # 稳定段，哈希会变，守卫当场抓住（见 tests/check_prompt_layers.py）。
        try:
            _sizes = prompt_layers.layer_sizes(
                SYSTEM_PROMPT, today_date=date.today(),
                summary_section=summary_section, l4_section=l4_section,
                skill_section=skill_section, context=context,
            )
            print(f"[L1 分层] {prompt_layers.describe_layer_sizes(_sizes)}")
            print(f"[L1 分层] 稳定前缀 stable_hash={prompt_layers.stable_hash(SYSTEM_PROMPT, date.today())}")
        except Exception as e:
            print(f"[L1 分层] 审计失败（不影响回答）: {e}")

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

    @staticmethod
    def _tool_result_ok(result: str) -> bool:
        """工具返回是否成功（技能库门禁 G2 的判据）。

        约定：工具失败时 JSON 里带非空 `error` 字段（见 `SYSTEM_PROMPT` 的统计工具规则）。
        非 JSON 结果无法判定，**保守视为成功** —— 宁可少记一条经验，也不误拦正常轨迹。
        """
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return True
        if isinstance(parsed, dict):
            return not parsed.get("error")
        return True

    def _answer_llm(self, route):
        """按**成本档位**选本轮"回答用"的客户端。默认返回 `self.llm`（行为不变）。

        ★ 只作用于「工具循环」与「最终回答」这两处 —— 标题 / 摘要 / L3 压缩 /
          L4 冲突消解等**内部维护**仍走生产客户端，避免记忆维护质量下降。

        三重保守（任一成立就回用生产模型）：
          ① 成本路由开关**默认关闭** → `model_for_route()` 返回 None；
          ② **L2 全链不在降档表里** → 返回 None（全链不许降质）；
          ③ 降档客户端构造失败（缺 key / 未登记）→ **捕获后回用生产模型**，
             绝不因为省钱而让用户答不了问题。
        """
        try:
            tier = providers.model_for_route(getattr(route, "level", ""))
        except Exception:                                    # noqa: BLE001
            return self.llm
        if tier is None:
            return self.llm
        prov, model = tier
        try:
            cli = providers.cached_client(prov, model)
        except Exception as e:                               # noqa: BLE001
            print(f"[L1 成本] 降档失败（{type(e).__name__}: {e}）→ 回用生产模型")
            return self.llm
        print(f"[L1 成本] 档位 {getattr(route, 'level', '?')} → 降档到 {prov}:{model}")
        return cli

    def _execute_tool_loop(
        self, messages: list[dict], tools: list[dict], model_name: str | None,
        *, trace: list[dict] | None = None, llm=None,
    ) -> list[dict]:
        """非流式工具调用循环: 反复请求 LLM 直到不再要求工具。返回更新后的 messages。

        Args:
            trace: 可选的**出参**（传入列表即生效）。每次工具调用追加一条
                `{"name", "params", "ok", "round"}`，供技能库登记轨迹使用。
                **只收集工具名与参数名，绝不收集参数值**（对应决策 D1）。
                默认 None 时行为与改动前完全一致（既有调用方无需改动）。
            llm: 可选的客户端覆盖（成本分层路由用）。默认 None = 用 `self.llm`，
                **行为与改动前完全一致**。
        """
        llm = llm or self.llm
        max_loops = 3  # 安全上限

        # ★ 三层防线（`core/tools/dispatch.py`）：
        #   ① 名字解析（别名归一 / 带候选的纠正）—— 在 `execute_tool_result` 里生效
        #   ② 参数多级降级 —— 本循环用它替换原来的静默 `{}`
        #   ③ 循环防护 —— 相同 (工具, 参数) 重复调用短路
        tool_dispatch = dispatch
        loop_guard = dispatch.LoopGuard(max_repeat=1)

        # 把本引擎的运行模式带进工具层 —— 单一闸门（core/tools/capabilities.py）
        # 据此判策略（如 Web 访客场景禁止会写盘的工具）。
        # 用 contextvar 而不是改签名：`_execute_tool` 被 tests/eval_numeric.py 以
        # **未绑定静态方法**方式调用（RAGEngine._execute_tool(...)），改签名会打破它。
        tool_caps.set_mode(self._owns_user_data)
        rounds_done = 0

        for round_no in range(max_loops):
            rounds_done = round_no + 1
            msg = llm.chat_with_tools(messages, tools, model_name)
            if msg is None:
                break

            # 如果没有工具调用，通常意味着模型已经准备好作答 → 退出，交给流式作答
            if not msg.tool_calls:
                content = getattr(msg, "content", "") or ""
                # ★ 防线三补：**空转回答**——模型说"任务已完成/无需更多任务"却**没给实质答案**。
                # 中文模型在收尾时高频出现（AgentGPT 的 `real_tasks_filter` 处理的是同一类问题）。
                # 这种回答直接交给流式作答的话，用户会拿到一句"任务已完成"。
                # 所以还剩轮次时**明确要求它重新作答**（只多花一次调用，且只在空转时发生）。
                if (dispatch.is_empty_response(content)
                        and round_no + 1 < max_loops):
                    log.warning("检测到空转回答，要求重新作答: %r", content[:40])
                    print(f"[Tool Loop] ★ 空转回答已拦截: {content[:40]!r}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": ("你上一条只是说任务结束了，但**没有给出用户要的实质内容**。"
                                    "请直接回答用户最初的问题；如果确实缺少信息，"
                                    "请明确说明缺什么、需要用户补充什么。"),
                    })
                    continue
                break

            # 【关键修复】：必须先将包含 tool_calls 的 assistant 消息追加到历史中
            messages.append(msg)

            # 轨迹录制（E4）：未开启时 `traj` 为 None，仅多一次判断
            traj = trajectory.current()
            traj_round = None
            if traj is not None:
                traj_round = traj.start_round(
                    content=str(getattr(msg, "content", "") or ""),
                    reasoning=_extract_reasoning(msg),
                )

            # 遍历并执行工具
            for tc in msg.tool_calls:
                func_name = tc.function.name

                # ★ 防线二：参数**多级降级解析**（原来这里是
                # `except json.JSONDecodeError: func_args = {}` —— 静默降级，
                # 既不记日志也不回灌，模型永远不知道自己发错了。见 `ORCHESTRATION.md` §1 缺陷 #3）。
                parsed = tool_dispatch.parse_tool_arguments(tc.function.arguments)
                if not parsed.ok:
                    # 解析失败 → **把错误当行动指引回灌**，让模型自己重发；
                    # 绝不拿空 dict 去执行（那会得到一个"参数全缺"的假失败）。
                    log.warning("工具参数解析失败: %s — %s", func_name, parsed.error)
                    print(f"[Tool Call] ★ 参数解析失败（{parsed.level}）: {func_name}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": parsed.error,
                    })
                    continue
                func_args = parsed.args
                if parsed.level != tool_dispatch.LEVEL_JSON:
                    log.info("工具参数走了降级解析: %s ← %s", func_name, parsed.level)
                    print(f"[Tool Call] 参数降级解析: {func_name} ← {parsed.level}")

                # ★ 防线三：**相同 (工具, 参数) 重复调用**防护 ——
                # 模型原地打转时每轮都在烧钱，且结果不会变。
                repeat_msg = loop_guard.check(func_name, func_args)
                if repeat_msg is not None:
                    log.warning("重复工具调用被短路: %s", func_name)
                    print(f"[Tool Call] ★ 重复调用已短路: {func_name}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": repeat_msg,
                    })
                    continue

                print(f"[Tool Call] 调用工具: {func_name}, 参数: {func_args}")
                log.info("调用工具: %s, 参数: %s", func_name, func_args)

                result = self._execute_tool(func_name, func_args)
                loop_guard.remember(func_name, func_args, result)

                # 轨迹录制（E4）
                if traj is not None and traj_round is not None:
                    tc_rec = traj.add_tool_call(
                        traj_round, name=func_name,
                        arguments=str(getattr(tc.function, "arguments", "") or ""),
                        args=func_args, call_id=str(getattr(tc, "id", "") or ""))
                    tc_rec.result = result
                    tc_rec.ok = self._tool_result_ok(result)

                summary = result[:300] + ("..." if len(result) > 300 else "")
                print(f"[Tool Call] 结果: {summary}")
                log.info("工具结果: %s", summary)

                if trace is not None:
                    trace.append({
                        "name": func_name,
                        "params": sorted(str(k) for k in func_args),
                        "ok": self._tool_result_ok(result),
                        "round": round_no,
                    })

                # 追加工具执行结果
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
        else:
            # ★★ `for...else` —— 循环**自然跑满**（没有被 break 打断），
            #    意味着**模型在最后一轮仍然要求调用工具**，但轮次已经没了。
            #
            #    在此之前，这个事实**完全丢失**：循环静默退出，
            #    然后照常走流式作答 —— 模型拿着不完整的中间结果
            #    组织出一段"看起来完整"的答案，用户不知道链条断了。
            #
            #    这是**静默失败**，与 `07` 号缺陷 3 记的是同一件事，
            #    但它此前被判断成"极端情况下产不出正文"，
            #    实测后修正为"**常规失效路径**"。触发判据见 `docs/HANDOFF.md`。
            loop_guard.mark_rounds_exhausted(max_loops)
            log.warning("工具循环轮次用尽，模型仍要求调用工具（wants_more）")
            print(f"[Tool Loop] ★ 轮次用尽（{max_loops} 轮）但模型仍需工具"
                  " —— 链条可能未走完")

        # ★ 把观测结果挂到实例上，供 `ask()` 决定是否移交 / 提示用户。
        #   用实例属性而不是改返回值：返回值是 `messages`，改它会影响所有既有调用方。
        #
        #   `rounds_used`：跑满时是 max_loops；中途 break 时是「实际跑过的轮数」。
        self._last_loop_wants_more = loop_guard.wants_more
        self._last_loop_rounds = (loop_guard.rounds_used or rounds_done)
        self._last_loop_completed = loop_guard.completed_calls()
        return messages

    # ------------------- 技能库登记 ---------------------------------

    def _record_skill_trajectory(
        self, trace: list[dict], *, question: str, answered: bool,
    ) -> None:
        """把本轮工具轨迹登记进技能库（零 LLM；门禁 G1–G6 在库内判定，失败不影响回答）。

        【为什么放在 ask() 末尾而不是工具循环里】门禁 G3 要求"模型最终产出了文字回答"，
        而回答由循环之后的**流式调用**产生 —— 循环内无法判定。放到 `ask()` 末尾才能
        区分"答完了"与"链路失败"。这正是评测方法缺陷 M4 的教训：API 失败曾被当成回答。
        """
        if not trace:
            return

        chain: list[str] = []
        arg_shape: dict[str, list[str]] = {}
        for item in trace:
            name = str(item.get("name", ""))
            chain.append(name)
            names = arg_shape.setdefault(name, [])
            for p in item.get("params") or []:
                if p not in names:
                    names.append(p)

        rounds = max((int(i.get("round", 0)) for i in trace), default=0) + 1
        try:
            outcome = self.crystallized.record_trajectory(
                chain, arg_shape,
                question=question,
                tool_ok=all(bool(i.get("ok")) for i in trace),
                answered=answered,
                rounds=rounds,
            )
            signature = ">".join(chain)
            print(f"[技能库] {outcome.action}: {signature} ({outcome.reason})")
        except Exception as e:
            print(f"[技能库] 登记失败（忽略，不影响回答）: {e}")

    # ------------------- 问答入口 (流式) --------------------------

    @staticmethod
    def _report_dir() -> str:
        """任务产物目录（**项目根下的绝对路径**）。

        ★ 为什么固定项目内、不做"用户工作区"：
          工作区是**新的信任面** —— 需要授权根、`..` 逃逸防护、符号链接防护、
          以及"读外部文件"的能力声明（`docs/PERCEPTION.md` §六）。
          在这些机制建成之前，给一个"选了目录就被限制住"的按钮只会带来
          **假安全感**。

        ★★ 为什么必须是**绝对路径**（2026-10-20 修）：
          第一版返回相对串 `"reports"`，于是产物落在**进程 cwd** 下 ——
          而用户完全可能从别处启动（`python E:/cotton_agent/main.py`），
          那一来：
            · 报告跑到一个用户根本找不到的目录；
            · 界面的链接白名单以 `cwd` 为根比较，**两边对不上**。
          用项目根算绝对路径，两处就都锚在同一个位置。
        """
        from pathlib import Path as _P
        return str(_P(__file__).resolve().parent.parent / "reports")

    def _persist_exchange(self, question: str, answer: str,
                          reasoning: str = "") -> None:
        """把一轮问答写进 L2 并落盘（**早返回路径与主路径共用**）。

        ★ 为什么单独抽出来：`/perception` 是在 `ask()` **最前面**拦截并 return 的，
          它也要记进会话 —— 否则用户在侧边栏看不到自己刚发过这条指令，
          下次续聊时上下文也是断的。
        """
        session = self._current_session()
        is_first_turn = (len(session.conversation_store) == 0)
        with self._lock:
            session.conversation_store.append(
                {"role": "user", "content": question})
            session.conversation_store.append(
                {"role": "assistant", "content": answer, "reasoning": reasoning})
            self._save_session_state()
        if is_first_turn and answer:
            self._generate_title(self.current_session_id, question, answer)

    def _perception_note(self, request) -> str:
        """处理一条 `/perception` 指令，返回要回显给用户的文本。

        ★ 会在**后台启动任务后短暂等待**报告（三节点全是本地计算、不调 LLM，
          实测 1–2 秒）。等到就把报告路径做成**可点链接**回给用户；
          等不到就如实说"还在跑"，绝不假装完成。
        """
        try:
            from core.perception import launch as _plaunch
        except Exception as e:                               # noqa: BLE001
            return f"\n\n---\n感知模块不可用：{type(e).__name__}: {e}\n"

        _out_dir = self._report_dir()
        print(f"[Perception] 产物目录: {_out_dir}")
        try:
            res = _plaunch.launch(request, out_dir=_out_dir)
        except Exception as e:                               # noqa: BLE001
            log.warning("感知任务启动异常: %s", e)
            return (f"\n\n---\n已识别为感知分析任务，但**启动时发生异常**："
                    f"{type(e).__name__}: {e}\n")

        if not res.ok:
            _miss = ("（缺：" + "、".join(f"`{m}`" for m in res.missing) + "）"
                     if res.missing else "")
            log.warning("感知任务未启动: %s", res.message)
            print(f"[Perception] ★ 未启动：{res.message}")
            return (f"\n\n---\n已识别为感知分析任务，但**未能启动**{_miss}。\n\n"
                    f"{res.message}\n\n{_plaunch.help_text()}\n")

        log.warning("感知任务已启动: run_id=%s demo=%s", res.run_id, res.demo)
        print(f"[Perception] 已启动 run_id={res.run_id} demo={res.demo}")

        _tag = "（**合成校准**，仅演示机制）" if res.demo else ""
        _head = (f"\n\n---\n"
                 f"已启动**感知分析任务**{_tag}。\n\n"
                 f"- **run_id**：`{res.run_id}`\n"
                 f"- **流程**：框数 → 密度 → 校准区间 → 四档门控 → Markdown 报告\n"
                 f"- **校准**：{res.message}\n")

        # ① 短暂等待报告（三节点本地计算，通常 1–2 秒）
        path = _plaunch.wait_for_report(res.run_id)
        if path:
            # ★★ 自检：产物必须在项目内的 reports/ 下。
            #   不在就是环境异常（例如跑了旧代码、或被临时目录劫持），
            #   这时**大声说**，别让用户点到死链还一头雾水。
            from pathlib import Path as _P2
            _real = _P2(path).resolve()
            _want = _P2(_out_dir).resolve()
            _ok_dir = False
            try:
                _real.relative_to(_want)
                _ok_dir = True
            except ValueError:
                pass
            if not _ok_dir:
                print(f"[Perception] ★★ 报告不在预期目录！"
                      f"实际 {_real} | 预期 {_want}")
            # ★ 用 Markdown 链接 —— 气泡在流式结束后会渲染成可点击元素，
            #   点击后由界面按**白名单路径**决定是否打开（见 ui/main_window.py）
            rel = path.replace("\\", "/")
            try:
                from pathlib import Path as _P
                rel = _P(rel).relative_to(_P.cwd()).as_posix()
            except (ValueError, OSError):
                pass
            return (_head +
                    f"\n**✅ 报告已生成**：[📄 打开报告]({rel})\n\n"
                    f"报告文件：`{rel}`\n")
        # ② 没等到 → 如实说
        return (_head +
                f"\n任务仍在后台执行（或超时）。报告会落在 "
                f"`{self._report_dir()}/` 目录下。\n")

    def ask(
        self, question: str,
        model_name: str | None = None,
        enable_thinking: bool = False,
        provider: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Generator[tuple[str, str], None, None]:
        """基于 RAG + 原生工具的增强问答 (流式输出).

        内部读写当前会话的 L2/L3, L4 和 RAG 检索全局共享。

        Args:
            provider: **显式指定服务商**（界面里从下拉框选了"某家的某个模型"）。
                ★ 为什么必须传：`model_name` 只是**请求体里的一个字符串**，
                  而 HTTP 客户端（base_url + API key）是**构造时**就定死的。
                  只传模型名不传服务商，就会拿**默认服务商**的端点和密钥
                  去请求**别的服务商**的模型 → 404 或"模型不存在"。
                `None` 时保持原行为（用 `self.llm`）。
        """
        self._trigger_l2_compression()

        # ★★★ 显式指令**在最前面拦截** —— 不经过 LLM（`docs/PERCEPTION.md` §10.7）
        #
        #   此前这段检查放在 `ask()` **末尾**，于是打一句 `/perception --demo` 会：
        #     ① 白跑最多 3 轮工具循环（3 次 LLM 调用）
        #     ② 再白跑一次完整流式作答（1 次 LLM 调用）
        #     ③ **然后**才启动感知任务
        #   显式指令根本不需要模型判断 —— 那既是浪费，也让演示变得又慢又贵。
        #
        #   ★ 放在这里还有第二个好处：**零误判风险更小**。
        #     越早拦截，被其他分支改写/误处理的机会越少。
        try:
            from core.perception import intent as _pintent
            _preq = _pintent.detect(question)
        except Exception:                                    # noqa: BLE001
            _preq = None
        if _preq is not None:
            _note = self._perception_note(_preq)
            yield ("content", _note)
            self._persist_exchange(question, _note)
            log.info("感知指令：已在入口处理（未经过 LLM）")
            return

        # 分级网关：**只判定一次**，供「消息组装」与「工具下发」共用 ——
        # 两处各判一遍会漂移（同一条问句在不同地方得到不同档位）。
        route = query_router.classify(question)
        messages = self._build_messages(question, route=route)

        # ★ 成本分层路由（`core/providers.py`，**默认关闭**）：
        # 只影响下面这两处「回答用户」的调用；标题/摘要/压缩等内部维护仍走生产模型。
        # 开关关闭或档位为 L2 时，`answer_llm` 就是 `self.llm` —— **行为与改动前逐字一致**。
        answer_llm = self._answer_llm(route)

        # ★★ 用户显式选了「某服务商的某模型」→ 用 (provider, model) 构造客户端。
        #    优先级高于成本分层路由（用户的手动选择不能被自动降档覆盖）。
        #    构造失败**不阻断问答**：打印后回退到默认客户端（Fail-Open 在这件事上是对的 ——
        #    选错模型不该让人连问题都问不了，但错误必须可见）。
        known_provider = str(getattr(self, "_provider_name", "") or "")
        if provider and model_name:
            try:
                answer_llm = providers.cached_client(provider, model_name)
                known_provider = provider
            except Exception as e:                             # noqa: BLE001
                print(f"[模型选择] 服务商 {provider!r} 不可用，回退默认：{e}")

        # ★ 轨迹录制（E4，**默认关闭**：`config.TRAJECTORY_PATH` 为空时 `recorder` 为 None）
        # 开启后本轮的工具调用 / 推理 / 用量会被完整记录，供导出 ShareGPT。
        # 关闭时只多一次空串判断 —— **行为与改动前逐字一致**。
        # ⚠️ 必须放在 `answer_llm` 之后：录制器要记模型名。
        recorder = None
        _traj_token = None
        if trajectory.enabled():
            try:
                recorder = trajectory.Recorder(
                    question=question,
                    model=str(getattr(answer_llm, "_model", "") or ""),
                    provider=str(getattr(self, "_provider_name", "") or ""),
                    system_prompt=_first_system_prompt(messages),
                    route=query_router.describe(route),
                )
                _traj_token = trajectory.set_current(recorder)
            except Exception:                                # noqa: BLE001
                recorder = None
                _traj_token = None

        # 工具调用循环（trace 收集本轮轨迹：工具名 + 参数名，供技能库登记）
        # skip_tools 时**完全不进入循环** —— 而不是"传空 tools 数组"：
        #   · 空 tools 的 API 兼容性不值得赌（可能报错或行为未定义）；
        #   · 直接跳过还**省下一次完整 LLM 调用**（比省 token 更值钱）；
        #   · 也从源头避免误调工具。
        trace: list[dict] = []
        if route.skip_tools:
            print("[L1 路由] 已跳过工具循环（省一次 LLM 调用 + schema token）")
        else:
            messages = self._execute_tool_loop(
                messages, self._get_local_tool_schemas(), model_name, trace=trace,
                llm=answer_llm)

        # 流式最终回答
        full_answer = ""
        full_reasoning = ""
        # ★ 未指定时用**产品默认档**（High）—— DEFAULT_REASONING_EFFORT 在
        #   core/llm_client.py，只在这里当回退，不下沉到 stream_response，
        #   以免标题/摘要等内部维护调用也被加价。
        _effort = reasoning_effort or DEFAULT_REASONING_EFFORT
        for msg_type, chunk in answer_llm.stream_response(
            messages, model_name=model_name, enable_thinking=enable_thinking,
            reasoning_effort=_effort,
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

        # ★★ 移交判定（`docs/HANDOFF.md`）——
        #   工具循环轮次用尽、而模型仍要求调用工具 = **依赖链没走完**。
        #   在此之前这里什么都不做：用户会拿到一段"看起来完整"的局部答案。
        #
        #   ★ 这里**生成 → 静态校验 → 真在后台执行**（不再只是"告知"）。
        #     走 human.confirm（等人签字）→ report.partial（落盘部分结果存档）。
        #     ★ 仍然的边界：**没有自动判断"剩下要查什么"** —— 那取决于中间结果，
        #     事前算不出来。所以移交做的是「固化现场 + 请人确认 + 输出存档」，
        #     而不是"自动补完"。
        # ★★ 显式工作流意图**优先于**移交（`docs/PERCEPTION.md` §5.4）
        #
        #   规则：**用户明确要的 > 系统自动推断的。**
        #   没有这条会出现这种错误引导 ——
        #     用户：「/perception 分析这批影像」
        #     → 模型试探调工具 → 3 轮花光
        #     → handoff 说「链条没走完，请把问题拆小」
        #     → 但用户要的是感知任务，不是拆小问题。
        #
        handoff_decision = None
        if getattr(self, "_last_loop_wants_more", False):
            handoff_decision = handoff.decide(
                wants_more=True,
                completed=getattr(self, "_last_loop_completed", []),
                question=question,
                rounds_used=getattr(self, "_last_loop_rounds", 0),
            )
            # ★★ 真执行（不再只是"告知"）——
            #   生成工作流 → 静态校验 → **后台启动**（不阻塞本流式回答）
            #   工作流会停在 `human.confirm` 等人签字；批准后落盘一份部分结果存档。
            outcome = handoff.execute(handoff_decision.inputs)
            note = handoff.partial_notice(
                handoff_decision.inputs,
                handoff_ok=outcome.ok,
                handoff_msg=(f"run_id={outcome.run_id}"
                             if outcome.run_id else outcome.error))
            full_answer += note
            yield ("content", note)
            if outcome.ok:
                log.warning("移交已执行: %s（run_id=%s，已完成 %d 步）",
                            handoff_decision.reason, outcome.run_id,
                            handoff_decision.inputs.get("done_count", 0))
                print(f"[Handoff] 已启动任务 run_id={outcome.run_id}"
                      f" —— 已完成 {handoff_decision.inputs.get('done_count', 0)} 步，"
                      f"等待人工确认")
            else:
                # ★ 移交没起来 → **如实回退**，绝不假装成功
                log.warning("移交未执行: %s", outcome.error)
                print(f"[Handoff] ★ 移交未执行：{outcome.error}")

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

        # 技能库登记（零 LLM）：门禁在库内判定；失败静默，不影响回答
        self._record_skill_trajectory(
            trace, question=question, answered=bool(full_answer.strip()),
        )

        # 异步 L4 实体记忆维护
        self._start_memory_worker(question, full_answer)

        # ★ 轨迹落盘（E4）：放在这里是因为此时 answer / reasoning / 用量都已确定。
        # 旁路设计：任何异常都不影响已经产生的回答。
        if recorder is not None:
            try:
                usage: dict = {}
                try:
                    u = answer_llm.last_usage
                    if u is not None:
                        usage = {
                            "prompt_tokens": getattr(u, "prompt_tokens", 0),
                            "completion_tokens": getattr(u, "completion_tokens", 0),
                            "cache_hit_tokens": getattr(u, "cache_hit_tokens", 0),
                            "cache_miss_tokens": getattr(u, "cache_miss_tokens", 0),
                            "calls": getattr(u, "calls", 0),
                        }
                except Exception:                            # noqa: BLE001
                    usage = {}
                record = recorder.finish(full_answer, full_reasoning, usage)
                written = trajectory.append_record(record)
                if written:
                    print(f"[轨迹] 已记录: {written}")
            except Exception as e:                           # noqa: BLE001
                print(f"[轨迹] 记录失败（忽略，不影响回答）: {e}")
            finally:
                if _traj_token is not None:
                    try:
                        trajectory.set_current(None)
                    except Exception:                        # noqa: BLE001
                        pass

        # token 用量（含缓存命中）—— 放在最后：此时本轮的工具选择 + 最终回答都已入账。
        # 这是「提示分层是否真的命中缓存」的唯一直接证据（见 core/prompt_layers.py）。
        try:
            if answer_llm.last_usage is not None:
                print(f"[L1 用量] {answer_llm.last_usage.describe()}")
        except Exception:
            pass
