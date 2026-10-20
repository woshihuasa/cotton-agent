# -*- coding: utf-8 -*-
"""记忆作用域判定：一条事实该进「画像库」还是「项目事实库」。

【为什么单独成一个模块】
        这个判据有**两个消费者**，必须只有一份实现：
        · 写入路径（`core/user_memory.py`）—— 每写一条事实就要判定一次
        · 存量治理（`tests/classify_l4.py`）—— 分家时对 540 条逐条判定
        两份实现各自演化，就会重演本项目一直在防的"两份真相"
        （与 `core/tools/schemas.py` 是工具契约的唯一来源同一个道理）。

【判据】按**作用域**分，不按"记忆的性质"分：
        > 换了一个生产季之后，这条事实还成立吗？成立 → 画像库；不成立 → 项目事实库。
        落到可机械判定的形式：
        1. **含农事阶段/时间锚点** → 项目事实（这一季发生了什么）
        2. **做法式且无强时间锚点** → 画像（"怎么做"换季仍成立）
        3. 其余（稳定属性/偏好/禁忌） → 画像

【两个坑，都是实测踩出来的（见 docs/PROCEDURAL_MEMORY.md v4/v5）】
        · **"提到了地区"≠"是关于地点的"** —— 曾据此把「阿拉尔20亩盐碱地」与
          「位于阿拉尔市」当成重复，差点误删 17 条真实信息。
        · **"提到了阶段"≠"是关于某一季的事实"** —— `用户吐絮期慎喷药并宜在傍晚打药`
          换季仍然成立，属画像。故做法式必须降级。

【刻意不 import core.rag_engine】后者会拉起 chromadb/tiktoken，而 `rag_engine` 反过来
        要 import `core.user_memory` —— 那样会构成**循环导入**。本模块只依赖 `re`。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 作用域名
PROFILE = "profile"          # 画像库：锁死单用户全局
PROJECT = "project"          # 项目事实库：锁死单项目（生产季）沙箱

# 集合名
PROFILE_COLLECTION = "user_profile"
PROJECT_COLLECTION = "project_facts"

# ---- 农事阶段 / 时间锚点 ----
STAGE_TOKENS = (
    "播种", "播前", "出苗", "苗期", "现蕾", "蕾期", "开花", "花铃期", "结铃",
    "吐絮", "打顶", "封顶", "停水", "停灌", "滴水", "冬灌", "春灌", "灌溉",
    "施肥", "基肥", "追肥", "化控", "脱叶", "催熟", "机采", "采收", "整地",
)
TIME_ANCHOR = re.compile(
    r"\d{4}年|\d{1,2}月|\d{1,2}日|[上中下]旬|今年|明年|本季|生产季|全季|全年"
)

# ---- 做法式表达（描述"怎么做"，而非"这一季发生了什么"）----
PRACTICE = re.compile(r"宜|慎|忌|避免|应当|应采用|采用|使用|偏好|不宜|注重")
# ---- 强时间锚点：出现它就说明说的是**某一次**，做法式降级不适用 ----
STRONG_TIME = re.compile(
    r"\d{4}年|\d{1,2}月|\d{1,2}日|本季|今年|明年|生产季|全季|当前|已|尚未"
)


def has_stage_anchor(text: str) -> bool:
    """是否含农事阶段或时间锚点。"""
    return bool(TIME_ANCHOR.search(text)) or any(t in text for t in STAGE_TOKENS)


def classify_scope(text: str) -> str:
    """判定一条事实的作用域，返回 `PROFILE` 或 `PROJECT`。

    注意：这是**写入路径**的判据，只关心"进哪个库"；
    存量治理还要判断"该不该删"（天气流水/题面回执/统计污染），那属于
    `tests/classify_l4.py` 的治理分类，不在本函数职责内。
    """
    text = text or ""
    if has_stage_anchor(text) and not (
        PRACTICE.search(text) and not STRONG_TIME.search(text)
    ):
        return PROJECT
    return PROFILE


# ---------------------------------------------------------------------------
# 可写性判据（只读挂载点门禁）
# ---------------------------------------------------------------------------

def is_under(path, root) -> bool:
    """`path` 是否落在 `root` 之内（含自身）。Windows 下**大小写不敏感**。

    用 `os.path.normcase` 而不是裸字符串比较 —— 同一路径在 Windows 上
    大小写可以不同，裸比较会漏判（这正是"门禁形同虚设"的常见成因）。
    """
    try:
        p = os.path.normcase(str(Path(path).resolve()))
        r = os.path.normcase(str(Path(root).resolve()))
    except (OSError, ValueError):
        return False
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def is_read_only_mount(path, mount_dirs) -> bool:
    """该路径是否落在**只读技能挂载点**内。

    挂载点＝人写的技能目录（随版本分发的 `skills/` + 外部挂载的技能仓库），
    真相源在 git。设计依据：`docs/ARCHITECTURE.md` §2（核心不变量）与 §4。

    **纯函数**：挂载点由调用方注入，本模块不 import config ——
    这样它既能被写入路径复用，也能零依赖地被守卫直接测试。
    """
    return any(is_under(path, d) for d in (mount_dirs or ()))