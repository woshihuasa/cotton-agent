# -*- coding: utf-8 -*-
"""工具结果的 JSON 序列化出口。

**为什么需要兜底**：pandas / numpy 的聚合结果是 numpy 标量，其中 `np.int64`
**不是** Python `int` 的子类，`json.dumps` 会抛
`Object of type int64 is not JSON serializable` → **整条工具链路失败**
（实测样本 #19 `stat='total'`）。而 `np.float64` 恰是 `float` 的子类，能静默
通过 —— 所以这类缺陷只在**整数聚合**路径暴露（ROADMAP 短板 #21）。

各工具已用 `float()` / `int()` 显式转换（治本）；本模块是**防御**：保证任何
遗漏只降级为"值被转换"，而不是"回答整体失败"。
"""
from __future__ import annotations

import json


def _json_default(o):
    """json.dumps 的 numpy 兜底。

    背景：pandas / numpy 的聚合结果是 **numpy 标量**，其中 `np.int64` **不是**
    Python `int` 的子类，`json.dumps` 会抛
    `Object of type int64 is not JSON serializable` → **整条工具链路失败**
    （实测样本 #19 `stat='total'`）。而 `np.float64` 恰是 `float` 的子类，
    能静默通过 —— 所以这类问题只在**整数聚合**时暴露。

    各工具模块已用 `float()` / `int()` 显式转换（治本）；此兜底是**防御**：
    保证任何遗漏只降级为"值被转换"，而不是"回答整体失败"。
    """
    import numpy as np

    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def dumps(obj, **kwargs) -> str:
    """工具结果统一的 JSON 序列化出口（ensure_ascii=False + numpy 兜底）。"""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", _json_default)
    return json.dumps(obj, **kwargs)
