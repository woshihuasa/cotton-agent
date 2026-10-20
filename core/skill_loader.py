# -*- coding: utf-8 -*-
"""手写技能加载器 —— 现扫挂载点、解析 `SKILL.md`、按触发词匹配、渲染成提示段落。

【定位】技能 = **以「提示注入」为加载机制、以「编排」为内容的程序性知识**
        （规范见 `docs/ARCHITECTURE.md` §8）。本模块做三件事：
        ① 现扫挂载点 ② 解析 frontmatter ③ 匹配 + 渲染成**文本**。

【三条红线（规范明确否决的）】
        · **不注册工具** —— 技能不是 Function Calling 工具，不碰 `core/tools/schemas.py`
          的单一真相。`tools_used` 只**声明**它编排了哪几个**现有**工具，加载器
          对照 `TOOL_SCHEMAS` 校验存在性（写错工具名当场报错）。
        · **不执行脚本** —— `scripts/` 是可选的**参考材料**，不是可执行入口。
        · **不建索引 / 不建注册表** —— 目录即真相，配置即挂载声明
          （`docs/ARCHITECTURE.md` §4 约定 1）。挂载点由 git 管，建索引就会陈旧。

【零 LLM】触发词匹配与结晶档共用 `trigger_hits()`，因此本层不产生任何 API 调用，
        可在 `_build_messages` 里安全调用（与结晶档同一个位置、同一条判据）。

【与结晶档的关系】两者都是"程序性知识"，都注入 prompt，但方向相反：
        · 结晶档 = **自下而上**（引擎观察到的轨迹，implicit）
        · 技能   = **自上而下**（人写的编排，explicit）
        晋升路径：结晶档里高命中的条目，人看过后写成技能。

【容错】单个技能文件解析失败**只跳过它并记录原因**，绝不让应用起不来
        （`errors()` 供守卫断言——"静默跳过"与"记录后跳过"是两回事）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.procedural_memory import MIN_TRIGGER_CHARS, trigger_hits

try:                                                   # pyyaml 是 langchain 的传递依赖
    import yaml
except ImportError:                                    # pragma: no cover
    yaml = None                                        # 所有技能都会报"无法解析 YAML"

# ---- 注入预算 ----
MAX_SKILLS = 2                     # 每轮最多注入几个技能
MAX_EXTERNAL_SKILLS = 1            # 其中**外部挂载**最多占几个槽（防它挤掉随版本分发的技能）
MAX_SKILL_SECTION_CHARS = 600      # 技能段落总长上限（硬上限）
CRYSTALLIZED_PRIORITY = 10         # 结晶档参与排序时使用的固定优先级

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

# 技能文件的硬性字段
_REQUIRED = ("id", "name", "triggers", "summary")
# 验收用例的字段
_ACCEPT_FIELDS = ("question", "expected_tools", "key_points")


@dataclass(frozen=True)
class Skill:
    """一个手写技能（解析后的不可变视图）。"""

    id: str
    name: str
    triggers: tuple[str, ...]
    tools_used: tuple[str, ...]
    summary: str
    body: str
    acceptance: tuple[dict, ...]
    inject_priority: int
    path: str          # 来源文件（诊断用）
    origin: str        # "shipped"（随版本）/ "external"（外部挂载）


@dataclass
class SkillLibrary:
    """技能库：现扫 + 解析 + 匹配。**不建索引、不写任何文件。**"""

    mount_dirs: list[Path] | None = None
    known_tools: set[str] | None = None
    _skills: list[Skill] = field(default_factory=list, init=False)
    _errors: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.reload()

    # ------------------------------------------------------------------
    # 现扫与解析
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """重新扫描全部挂载点（幂等；不写任何文件）。"""
        self._skills = []
        self._errors = []
        known = self.known_tools if self.known_tools is not None else _default_known_tools()

        for index, root in enumerate(self.mount_dirs or []):
            root = Path(root)
            origin = "shipped" if index == 0 else "external"
            if not root.is_dir():
                # 挂载点缺失**不是错误** —— 不阻断启动（与 config 的约定一致）
                continue
            # 只扫**顶层** `*.md`：`<skill_id>/scripts/` 下的说明不该被当成技能
            for md in sorted(root.glob("*.md")):
                try:
                    self._skills.append(self._parse(md, origin, known))
                except _SkillError as e:
                    self._errors.append(f"{md.name}: {e}")
                except Exception as e:                     # noqa: BLE001
                    self._errors.append(f"{md.name}: 未预期异常 {type(e).__name__}: {e}")

    def _parse(self, path: Path, origin: str, known: set[str]) -> Skill:
        text = path.read_text("utf-8")
        m = _FRONTMATTER.match(text)
        if not m:
            raise _SkillError("缺少 YAML frontmatter（文件须以 --- 开头、以 --- 结束）")
        if yaml is None:
            raise _SkillError("无法解析 YAML：未安装 pyyaml")

        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            raise _SkillError(f"frontmatter 不是合法 YAML: {e}") from e
        if not isinstance(meta, dict):
            raise _SkillError("frontmatter 必须是键值映射")

        for key in _REQUIRED:
            if not meta.get(key):
                raise _SkillError(f"缺必填字段 `{key}`")

        triggers = _norm_list(meta.get("triggers"))
        if not triggers:
            raise _SkillError("`triggers` 不能为空")
        short = [t for t in triggers if len(t) < MIN_TRIGGER_CHARS]
        if short:
            raise _SkillError(f"`triggers` 含单字标签 {short}（无区分度，会被丢弃）")

        tools_used = _norm_list(meta.get("tools_used"))
        unknown = [t for t in tools_used if t not in known]
        if unknown:
            raise _SkillError(
                f"`tools_used` 引用了不存在的工具 {unknown}"
                f"（技能只能编排现有工具，见 core/tools/schemas.py）"
            )

        acceptance = []
        for i, case in enumerate(meta.get("acceptance") or [], 1):
            if not isinstance(case, dict):
                raise _SkillError(f"`acceptance` 第 {i} 条不是映射")
            miss = [k for k in _ACCEPT_FIELDS if not case.get(k)]
            if miss:
                raise _SkillError(f"`acceptance` 第 {i} 条缺字段 {miss}")
            acceptance.append(dict(case))

        body = _strip_h1(m.group(2).strip())
        if not body:
            raise _SkillError("正文为空（技能内容就是编排，不能只有 frontmatter）")

        return Skill(
            id=str(meta["id"]),
            name=str(meta["name"]),
            triggers=tuple(triggers),
            tools_used=tuple(tools_used),
            summary=str(meta.get("summary") or "").strip(),
            body=body,
            acceptance=tuple(acceptance),
            inject_priority=int(meta.get("inject_priority") or 0),
            path=str(path),
            origin=origin,
        )

    # ------------------------------------------------------------------
    # 只读访问
    # ------------------------------------------------------------------

    def all_skills(self) -> list[Skill]:
        """全部已加载技能（拷贝列表）。"""
        return list(self._skills)

    def errors(self) -> list[str]:
        """解析失败记录 —— **记录后跳过，而不是静默吞掉**。"""
        return list(self._errors)

    # ------------------------------------------------------------------
    # 匹配与渲染（零 LLM）
    # ------------------------------------------------------------------

    def match(self, question: str, k: int = MAX_SKILLS) -> list[Skill]:
        """按问句匹配技能。

        【排序】命中标签数降序 → **来源档位**（shipped 优先）→ `inject_priority` 降序
        → `id` 升序（稳定、可复现）。命中数为 0 的技能一律不返回。

        【外部配额】外部挂载的技能最多占 `MAX_EXTERNAL_SKILLS` 个槽。
        理由见 `docs/ARCHITECTURE.md` §9：在「提示注入」架构下，第三方技能的
        **唯一威胁就是往 prompt 里注入文本** —— 而"挤掉可信技能、垄断注入槽"
        正是它最省力的攻击方式。配额让可信内容始终有位置。
        """
        if not (question or "").strip() or k <= 0:
            return []
        scored = [
            (trigger_hits(question, s.triggers),
             0 if s.origin == "shipped" else 1,
             s.inject_priority, s.id, s)
            for s in self._skills
        ]
        scored = [x for x in scored if x[0] > 0]
        scored.sort(key=lambda x: (-x[0], x[1], -x[2], x[3]))

        out: list[Skill] = []
        external_used = 0
        for _hits, tier, _pri, _sid, sk in scored:
            if tier == 1:
                if external_used >= MAX_EXTERNAL_SKILLS:
                    continue                      # 配额已满 → 跳过，让位给可信技能
                external_used += 1
            out.append(sk)
            if len(out) >= k:
                break
        return out

    def format_section(self, skills) -> str:
        """渲染成注入 `SYSTEM_PROMPT` 的段落（含**反锁死**声明与硬长度上限）。

        预算策略：先给完整正文；超预算后改用 `summary`；再超就停止。
        这样"匹配到很多技能"不会把 prompt 撑爆。
        """
        skills = list(skills or [])
        if not skills:
            return ""

        tail = "若与当前问题不符, 以当前问题为准, 不要照搬上述步骤.\n\n"
        head = "【技能 · 编排指导】(人写的处理路径, 供参考)\n"
        text = head
        for s in skills:
            # 外部来源**必须显式标注**：这是「提示注入」架构下唯一有效的对策 ——
            # 让模型知道这段内容未经本项目审查（docs/ARCHITECTURE.md §9）。
            label = f"── {s.name} ──"
            if s.origin != "shipped":
                label += "  ⚠️ 来源: 外部挂载, 未经本项目审查"
            full = f"{label}\n{s.body}\n"
            lean = f"{label} {s.summary}\n"
            for block in (full, lean):
                if len(text) + len(block) + len(tail) <= MAX_SKILL_SECTION_CHARS:
                    text += block
                    break
            else:
                break                      # 两种都放不下 → 停止追加
        if text == head:
            return ""
        return text + tail

    def format_for_question(self, question: str, k: int = MAX_SKILLS) -> str:
        """匹配 + 渲染的便捷入口（接线时一行可调）。"""
        return self.format_section(self.match(question, k=k))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

class _SkillError(Exception):
    """技能文件不合法（**只跳过该文件**，不影响其它技能与应用启动）。"""


def _norm_list(value) -> list[str]:
    """把 YAML 里的标量或列表统一成去重、去空白的字符串列表。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def _strip_h1(body: str) -> str:
    """去掉正文的 H1 标题 —— 它在注入段落里与技能名重复。"""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line.lstrip().startswith("# "):
            lines = lines[i + 1:]
        break
    return "\n".join(lines).strip()


def _default_known_tools() -> set[str]:
    """现有工具名集合（**懒加载** `core.tools`，避免本模块被拖入重依赖）。"""
    from core.tools import TOOL_SCHEMAS

    return {t["function"]["name"] for t in TOOL_SCHEMAS}