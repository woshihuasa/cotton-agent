# -*- coding: utf-8 -*-
"""技能库 —— L4 长期记忆容器的第三个库：工具调用轨迹的经验沉淀。

【定位】L4 长期记忆容器按**作用域**分三库（详见 `docs/PROCEDURAL_MEMORY.md`）：

        | 库 | 内容 | 作用域（锁死对象） | 位置 |
        |---|---|---|---|
        | 画像库 | 关于"这个人"的稳定属性 | 单用户全局 | 运行区 |
        | 项目事实库 | 关于"这个项目"的事实 | 单项目沙箱 | 运行区 |
        | **技能结晶档**（本模块） | 关于"这类事怎么做"的路线 | 本机私有 | **运行区** |
        | 手写技能 | 人写的技能包（说明 / 契约 / 脚本） | 随版本分发 | **源码区**（只读挂载） |

        > ⚠️ **本模块只管结晶档**（引擎自动积累）。"随包分发"这件事由**手写技能**
        > （源码区 `skills/`）承担，不是本模块 —— 结晶档是本机私有的运行态数据。
        > 详见 `docs/ARCHITECTURE.md`。

        本模块只实现**技能库**。它**不依赖** chromadb / embedding / LLM，
        也与 `core/user_memory.py`（画像库 + L3 摘要）没有任何往来 —— 三个"不共用"：

        · **清理不共用**：技能库跨用户、跨项目保留，只按容量淘汰（不随会话/项目删除）
        · **写入规则不共用**：技能库必须确定性去重；画像库是覆盖式更新
        · **抽取手段不共用**：技能库零 LLM 直接读轨迹；画像库走大模型抽取

【经验条目】= 一次**成功工具调用轨迹**的抽象：
        *什么问法特征(triggers) → 走了哪条工具链(signature) → 参数长什么样(arg_shape)*

        只记**参数名**，绝不记参数值、结果摘要或用户数据（对应决策 **D1 = (a)**）。
        因此条目不含任何用户信息 —— 这是它能"随包分发、甚至预置出厂种子"的前提。

【六道写入门禁（全部确定性、零 LLM）】
        G1 工具链非空 · G2 全链路无错误 · G3 模型产出了最终回答
        G4 签名归一化去重（同一路线只登记一次，再次出现只累加 hit_count）
        G5 容量上限 + 淘汰（默认 50 条）· G6 同签名变体封顶（默认 3）

【为什么门禁要这么严】画像库曾膨胀到 624 条，根因是"**连逐字相同的事实都发 LLM 判定**"，
        而事后实测真正可安全归并的只有 13%~17%（ROADMAP S6 自我更正）。
        技能库从第一天起就立规矩：**一模一样的路线用字符串相等去重** ——
        纯机械、可单测、不依赖任何判断力。

【存储】单文件 JSONL（`app_data_dir()/procedural_memory/index.jsonl`），原子写 + 进程内锁。
        结晶条目是**纯元数据**（工具链 + 参数名 + triggers + 命中次数），不会携带脚本或
        附件，故单文件即全部 —— **不建子目录**（docs/ARCHITECTURE.md §3.2）。
        不放进向量库的理由：条目数天然很小（工具仅 6 个、链长 ≤ 3），
        向量检索杀鸡用牛刀，还要为每次检索付 embedding 费用；且记忆库目录
        每次打开字节内容都会漂移（ROADMAP S2 记录），不利于逐条断言。
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.memory_scope import is_read_only_mount

# ---- 运行区路径（单一真相：引擎、接线与守卫共用，避免多处写死）----
RUNTIME_SUBDIR = "procedural_memory"   # 运行区目录（位于 app_data_dir 下）
INDEX_NAME = "index.jsonl"             # 结晶档条目的**全部内容**

# ---- 容量与上限（实例化时可覆盖，便于守卫用小数字快速构造边界） ----
MAX_ENTRIES = 50                 # G5 技能库总容量
MAX_VARIANTS_PER_SIGNATURE = 3   # G6 同一工具链的形状变体数
MAX_TRIGGERS_PER_ENTRY = 8       # 单条条目的检索标签上限
MIN_TRIGGER_CHARS = 2            # 检索标签最短长度：单字（"图""方""地"）无区分度，一律丢弃
MATCH_TOP_K = 2                  # 每轮注入的条目数上限（硬上限）
MAX_DESC_CHARS = 90              # 单条描述的字符上限
MAX_SECTION_CHARS = 300          # 注入段落的总长上限（硬上限）

# 疑问套话：整段命中即丢弃，避免"这类词每个问题都有"导致全量误命中
_STOP_SEGMENTS = (
    "请问", "帮我", "一下", "怎么样", "怎么办", "为什么", "什么", "多少", "哪些",
    "是否", "可以", "能不能", "要不要", "适合", "今天", "明天", "现在", "最近",
    "我的", "我们", "这个", "那个", "如何", "时候",
)

# 数值锚点：年份与带单位的量。经验上这是最可区分的检索信号
_NUMERIC_ANCHOR = re.compile(r"\d{4}|\d+(?:\.\d+)?(?:亩|方|公斤|吨|元|℃|%)")
_SPLIT_PATTERN = re.compile(r"[，。！？；：、,.!?;:\s（）()\[\]【】“”\"']+")


@dataclass(frozen=True)
class RecordOutcome:
    """登记结果（供调用方与守卫断言）。

    Attributes:
        action: "added"（新增）| "hit"（命中已有，累加次数）| "rejected"（被门禁拒绝）
        entry_id: 相关条目 ID；rejected 时为空串
        reason: 机器可读的原因标签 ——
                empty_chain / tool_error / no_answer / duplicate_hit / variant_cap / added
    """

    action: str
    entry_id: str
    reason: str


# ---------------------------------------------------------------------------
# 归一化与提取（模块级纯函数，便于单独测试）
# ---------------------------------------------------------------------------

def normalize_signature(chain) -> str:
    """有序工具链 → 签名（用 `>` 连接）；空白项被丢弃。

    >>> normalize_signature(["get_weather", "query_cotton_stats"])
    'get_weather>query_cotton_stats'
    """
    parts = [str(t).strip() for t in (chain or []) if str(t).strip()]
    return ">".join(parts)


def normalize_shape(arg_shape) -> str:
    """参数形状 → 确定性指纹。**只有参数名，没有参数值。**

    工具名与参数名均排序，因此与书写顺序无关；同一个工具链的不同形状
    会得到不同指纹（这是 G6"变体封顶"的判定基础）。
    """
    if not isinstance(arg_shape, dict):
        return ""
    segs: list[str] = []
    for tool in sorted(str(k) for k in arg_shape):
        raw = arg_shape.get(tool) or []
        names = sorted({str(n).strip() for n in raw if str(n).strip()})
        segs.append(f"{tool}:{','.join(names)}")
    return "|".join(segs)


def entry_key(entry: dict) -> str:
    """条目的去重主键 = 签名 + 参数形状指纹。

    `shape_key` **不落盘**，每次都从 `arg_shape` 现算 —— 保持单一真相，
    避免"存了一份指纹、改了一处忘了改另一处"。
    """
    return f"{entry.get('signature', '')}#{normalize_shape(entry.get('arg_shape'))}"


def extract_triggers(question: str, max_n: int = MAX_TRIGGERS_PER_ENTRY) -> list[str]:
    """从问句中提取检索标签（确定性、零 LLM）。

    【策略】零成本的第一近似，只用两种信号：
        1. **数值锚点** —— 年份，以及带单位的量（亩/方/公斤/吨/元/℃/%），最可区分
        2. **按标点切段后的实义片段**（2–10 字），整段是疑问套话的丢弃

    【已知局限】不做分词、不做俗名/学名归一 —— 与知识库"红蜘蛛 vs 棉叶螨"
        属同一类边界（ROADMAP 短板 #21）。若检索质量不达标，再评估引入领域词表。
    """
    text = (question or "").strip()
    if not text:
        return []

    out: list[str] = []

    for m in _NUMERIC_ANCHOR.findall(text):
        if m not in out:
            out.append(m)

    for seg in _SPLIT_PATTERN.split(text):
        seg = seg.strip()
        if not seg or seg in _STOP_SEGMENTS:
            continue
        for word in _STOP_SEGMENTS:
            seg = seg.replace(word, " ")
        for piece in seg.split():
            if 2 <= len(piece) <= 10 and piece not in out:
                out.append(piece)

    return out[:max_n]


def _normalize_triggers(triggers) -> list[str]:
    """去重、去空白、**丢弃单字标签**、截断到上限，保持原始顺序。

    为什么要丢单字：中文里"图""方""地"这类单字几乎任何问句都可能出现，
    保留它们会让匹配退化成"全都命中"（守卫实测：`趋势/图` 曾靠"图"压过
    `产量/统计` 的正确排序）。因此这里与 `extract_triggers` 的最短长度保持一致。
    """
    out: list[str] = []
    for t in (triggers or []):
        t = str(t).strip()
        if len(t) < MIN_TRIGGER_CHARS:
            continue
        if t not in out:
            out.append(t)
        if len(out) >= MAX_TRIGGERS_PER_ENTRY:
            break
    return out


def trigger_hits(question: str, triggers) -> int:
    """问句命中了多少个触发标签（子串匹配，零 LLM）。

    **结晶档与手写技能共用这一条判据** —— 两处各写一套匹配规则就会漂移，
    而本项目一直在防的正是"两份真相"（见 `core/skill_loader.py` 的用法）。
    """
    q = (question or "").strip().lower()
    if not q:
        return 0
    return sum(1 for t in (triggers or []) if t and str(t).strip().lower() in q)


def _merge_triggers(old, new) -> list[str]:
    """并集：旧标签在前，新标签补充在后。"""
    return _normalize_triggers(list(old or []) + list(new or []))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 技能库
# ---------------------------------------------------------------------------

class ProceduralMemoryStore:
    """技能库（程序记忆）：单文件 JSONL，确定性门禁 + 确定性检索。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        mount_dirs=None,
        max_entries: int = MAX_ENTRIES,
        max_variants: int = MAX_VARIANTS_PER_SIGNATURE,
    ) -> None:
        """初始化技能库。

        Args:
            path: 存储文件路径。None 时使用
                  `app_data_dir()/procedural_memory/index.jsonl`（生产路径）。
                  传入其它路径即可得到**隔离作用域**（评测沙箱 / 守卫临时目录）。
            mount_dirs: **只读技能挂载点**（人写的技能目录）。`path` 落在其中时
                  构造即拒绝 —— 手写技能的真相源在 git，引擎绝不可写。
                  默认 None：不注入即不做该检查（守卫可直接测试这条门禁）。
            max_entries: G5 容量上限。守卫可传小数字以低成本构造淘汰边界。
            max_variants: G6 同签名变体上限。

        Note:
            显式传入 path 时**不会** import config，因此不会触碰生产目录 ——
            这是守卫能够在沙箱里安全运行的前提（本模块**零第三方依赖**）。
        """
        if path is None:
            from config import app_data_dir      # 延迟导入：避免 import 期建目录

            path = Path(app_data_dir()) / RUNTIME_SUBDIR / INDEX_NAME

        self._path = Path(path)

        # 只读挂载点门禁：手写技能随版本分发、由 git 管理，引擎**只读**。
        # 规则落在这里（而不是靠调用方自觉），并且有守卫反例在跑 ——
        # 一句 `if` 比结构保证弱，所以必须能被检查（docs/ARCHITECTURE.md §5.5）。
        if is_read_only_mount(self._path, mount_dirs):
            raise PermissionError(
                f"拒绝在只读技能挂载点内打开可写存储: {self._path}"
                "（手写技能的真相源在 git，见 docs/ARCHITECTURE.md §4）"
            )

        self._max_entries = int(max_entries)
        self._max_variants = int(max_variants)
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._skipped_lines = 0
        self._load()

    # ---- 只读辅助 ----

    @property
    def store_path(self) -> Path:
        """当前存储文件路径（供日志与隔离验证使用）。"""
        return self._path

    @property
    def skipped_lines(self) -> int:
        """载入时被跳过的坏行数（非 0 说明文件曾受损，应当告警而不是静默）。"""
        return self._skipped_lines

    def counts(self) -> dict[str, int]:
        """条目数（供守卫与隔离验证使用，与 `UserMemoryStore.counts()` 同形）。"""
        return {"entries": len(self._entries)}

    def all_entries(self) -> list[dict]:
        """全部条目的浅拷贝列表（供守卫逐条断言，调用方修改不影响库）。"""
        with self._lock:
            return [dict(e) for e in self._entries]

    # ---- 持久化 ----

    def _load(self) -> None:
        """载入 JSONL。坏行跳过并计数，不抛异常（与项目"降级不崩"的取向一致）。"""
        self._entries = []
        self._skipped_lines = 0
        if not self._path.exists():
            return
        for line in self._path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self._skipped_lines += 1
                continue
            if isinstance(obj, dict) and obj.get("id") and obj.get("signature"):
                self._entries.append(obj)
            else:
                self._skipped_lines += 1

    def _save(self) -> None:
        """原子写：先写临时文件再替换，进程中途崩溃不会留下半个文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for e in self._entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def _next_id(self) -> str:
        used = {e.get("id") for e in self._entries}
        n = len(used) + 1
        while f"exp_{n:04d}" in used:
            n += 1
        return f"exp_{n:04d}"

    # ---- G5 容量淘汰 ----

    def _enforce_capacity(self) -> list[str]:
        """超出容量时按 `hit_count` 升序 → `last_seen` 升序淘汰，返回被淘汰的 ID。

        新条目 `last_seen` 最新，因此在同为 0 次命中时**最后**被淘汰。
        """
        evicted: list[str] = []
        while len(self._entries) > self._max_entries:
            victim = min(
                self._entries,
                key=lambda e: (int(e.get("hit_count", 0)), str(e.get("last_seen", ""))),
            )
            self._entries.remove(victim)
            evicted.append(str(victim.get("id", "")))
        return evicted

    # ---- 写入路径（六道门禁） ----

    def record_trajectory(
        self,
        chain,
        arg_shape=None,
        *,
        question: str = "",
        triggers=None,
        tool_ok: bool = True,
        answered: bool = True,
        rounds: int = 1,
    ) -> RecordOutcome:
        """登记一次工具调用轨迹，经六道门禁后决定是否入库。

        Args:
            chain: **有序**工具名列表，如 `["get_weather", "query_cotton_stats"]`。
            arg_shape: `{工具名: [参数名, ...]}` —— **只传参数名，不要传参数值**。
            question: 原始问句；未显式给 triggers 时用它提取检索标签。
            triggers: 显式指定检索标签（优先于 question 提取）。
            tool_ok: G2 —— 本次工具调用是否全部成功。
            answered: G3 —— 模型是否最终产出了文字回答。
            rounds: 工具循环轮次（仅记录，用于诊断）。

        Returns:
            RecordOutcome：action 为 added / hit / rejected，reason 说明原因。
        """
        # G1 工具链非空
        signature = normalize_signature(chain)
        if not signature:
            return RecordOutcome("rejected", "", "empty_chain")

        # G2 全链路无错误
        if not tool_ok:
            return RecordOutcome("rejected", "", "tool_error")

        # G3 产出了最终回答
        if not answered:
            return RecordOutcome("rejected", "", "no_answer")

        shape = arg_shape if isinstance(arg_shape, dict) else {}
        trig = _normalize_triggers(triggers) if triggers else extract_triggers(question)
        now = _utc_now()

        with self._lock:
            candidate = {"signature": signature, "arg_shape": shape}
            key = entry_key(candidate)

            # G4 签名归一化去重：命中已有 → 只累加次数，绝不新增
            for e in self._entries:
                if entry_key(e) == key:
                    e["hit_count"] = int(e.get("hit_count", 0)) + 1
                    e["last_seen"] = now
                    e["triggers"] = _merge_triggers(e.get("triggers"), trig)
                    self._save()
                    return RecordOutcome("hit", str(e.get("id", "")), "duplicate_hit")

            # G6 同签名变体封顶
            variants = [e for e in self._entries if e.get("signature") == signature]
            if len(variants) >= self._max_variants:
                return RecordOutcome("rejected", "", "variant_cap")

            entry = {
                "id": self._next_id(),
                "signature": signature,
                "triggers": trig,
                "arg_shape": shape,
                "outcome": {
                    "ok": True,
                    "rounds": int(rounds),
                    "tool_calls": len(signature.split(">")) if signature else 0,
                },
                "hit_count": 0,
                "created_at": now,
                "last_seen": now,
            }
            self._entries.append(entry)

            # G5 容量上限
            self._enforce_capacity()
            self._save()
            return RecordOutcome("added", entry["id"], "added")

    # ---- 读取路径（确定性检索，零 LLM） ----

    def match(self, question: str, k: int = MATCH_TOP_K) -> list[dict]:
        """按问句匹配最相关的条目（**只读**，不改动 hit_count 与 last_seen）。

        排序：命中标签数降序 → `hit_count` 降序 → `last_seen` 降序；
        三者全同则保持文件顺序（稳定排序，结果可复现）。

        Args:
            question: **用户原始问句**（刻意不走查询改写，因此不产生 LLM 调用）。
            k: 返回条数上限，默认 `MATCH_TOP_K`。

        Returns:
            匹配到的条目浅拷贝列表；命中数为 0 的条目一律不返回。
        """
        q = (question or "").strip().lower()
        if not q or k <= 0:
            return []

        scored: list[tuple[int, int, str, dict]] = []
        with self._lock:
            for e in self._entries:
                hits = trigger_hits(q, e.get("triggers"))
                if hits > 0:
                    scored.append(
                        (hits, int(e.get("hit_count", 0)), str(e.get("last_seen", "")), e)
                    )

        scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return [dict(x[3]) for x in scored[:k]]

    # ---- 注入段落 ----

    def _describe(self, entry: dict) -> str:
        """单条条目的一句话描述（受 `MAX_DESC_CHARS` 约束）。"""
        trig = "/".join((entry.get("triggers") or [])[:4]) or "（无标签）"
        desc = f"{trig} → {entry.get('signature', '')}"
        if len(desc) > MAX_DESC_CHARS:
            desc = desc[: MAX_DESC_CHARS - 1] + "…"
        return desc

    def format_section(self, entries) -> str:
        """把条目渲染成注入系统提示的段落（含**反锁死**声明与硬长度上限）。

        三条必须齐全，缺一不可：
            1. 声明性质 —— 这是历史处理路径，**不是答案**
            2. 反锁死   —— 与当前问题不符时**以当前问题为准**，不要照搬
            3. 硬上限   —— 条数由调用方限定，总长 ≤ `MAX_SECTION_CHARS`

        超长时从**末位条目**开始丢弃，保证段落永远不超上限。
        """
        entries = list(entries or [])
        if not entries:
            return ""

        head = "【历史处理路径】(经验提示, 不是答案)"
        tail = "若与当前问题不符, 以当前问题为准, 不要照搬上述路径."

        while entries:
            body = [f"{i}. {self._describe(e)}" for i, e in enumerate(entries, 1)]
            text = "\n".join([head, *body, tail]) + "\n\n"
            if len(text) <= MAX_SECTION_CHARS:
                return text
            entries.pop()
        return ""

    def format_for_question(self, question: str, k: int = MATCH_TOP_K) -> str:
        """检索 + 渲染的便捷入口（接线时一行可调）。"""
        return self.format_section(self.match(question, k=k))