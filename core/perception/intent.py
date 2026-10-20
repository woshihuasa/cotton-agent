# -*- coding: utf-8 -*-
"""感知任务意图识别。

【为什么 V1 只认显式指令】

    本项目在"词面判定"上栽过两次，教训写在 `core/tools/dispatch.py`：
      · `is_refusal` 子串匹配 → 阳性对照召回 **0%**
      · 空转判定用"包含" → **误杀真回答**（"任务已完成，产量 125 公斤"）

    所以本模块**刻意只识别一种信号**：用户显式打出 `/perception`。
    它零歧义、零误判 —— 用户说了才做，没说就不做。

    ★ **"模型在交流中确认后执行"这条路径留到 V2**，理由：
      那需要模型判断"用户是不是想做感知分析"，而这类判断一旦错，
      后果是**给一个普通问题启动一个几分钟的任务**。
      V2 做它时必须配 `human.confirm`（已实现且测过：拒绝即熔断）。

【为什么不用"关键词 + 影像名词"的组合】

    试过，但"分析这批影像"与"影像质量对检测的影响是什么"在词面上几乎一样，
    前者要跑任务、后者是知识问答。**判据落在词面上就必然有一边要错。**
    V2 若要做，应当落在**结构信息**上（例如用户已指定工作区且目录里确实有影像）。

【返回值】
    识别到时返回 `PerceptionRequest`；否则 `None`。
    **绝不返回"可能是"** —— 模棱两可一律当"不是"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 显式指令前缀（大小写不敏感，允许前后空格）
COMMAND_PREFIX = "/perception"

#: 允许同义写法 —— 仍然必须是**显式命令**，不是自然语言
_ALIASES = ("/perception", "/感知", "/pc")

_RE_CMD = re.compile(
    r"^\s*(?:" + "|".join(re.escape(a) for a in _ALIASES) + r")(?:\s+(.*))?$",
    re.IGNORECASE | re.DOTALL)


@dataclass
class PerceptionRequest:
    """一次显式感知指令的解析结果。"""

    #: 指令后面的其余文字（可能是"帮我看看这块地"之类，原样保留供展示）
    trailing: str = ""
    #: 原始问题
    raw: str = ""
    #: 命中的命令词（便于日志与测试）
    command: str = ""
    #: 解析出的键值参数（`/perception n_boxes=1414 altitude=5`）
    params: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        n = len(self.params)
        return f"感知任务（{self.command}" + (f"，{n} 个参数）" if n else "）")


_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def detect(question: str) -> PerceptionRequest | None:
    """识别显式感知指令。**识别不到就返回 `None`，不猜。**"""
    if not isinstance(question, str) or not question.strip():
        return None
    m = _RE_CMD.match(question)
    if not m:
        return None
    rest = (m.group(1) or "").strip()
    params: dict[str, str] = {}
    for k, v in _KV_RE.findall(rest):
        params[k] = v
    cmd = question.strip().split()[0].lower()
    return PerceptionRequest(trailing=rest, raw=question, command=cmd,
                             params=params)


def strip_command(question: str) -> str:
    """去掉命令前缀，返回剩余文字（用于标题/日志）。"""
    r = detect(question)
    if r is None:
        return question
    return r.trailing or ""


__all__ = ["COMMAND_PREFIX", "PerceptionRequest", "detect", "strip_command"]
