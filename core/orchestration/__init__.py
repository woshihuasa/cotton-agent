# -*- coding: utf-8 -*-
"""编排层 —— 把"一步步调工具"变成**可声明、可校验、可续跑**的工作流。

【这一层的定位】它是一条**并行第二路径**，**不替代**现有的工具循环
        （`rag_engine._execute_tool_loop`）。问答照旧走原路径；
        只有明确写成工作流的任务才进这里。

【为什么值得做】
        1. **可声明**：流程写在 YAML 里，能 diff、能 code review、能被守卫校验，
           而不是藏在 prompt 里"希望模型照着走"；
        2. **可校验**：环、孤立节点、未知节点类型、必填缺失 —— **静态就能查出来**，零成本；
        3. **可续跑**：人工确认/长任务可以落盘断点，重新载入后**不重复执行已完成的节点**。

【P0 / P1 的边界】
        · **P0**（本次）：`model` + `loader` + `catalog` + `validator` ——
          **一份 YAML 能被校验并展开成含 `tool.*` 的节点图**，全程零 LLM、零网络；
        · **P1**：`engine` + `store` + `gates` —— 端到端跑通一个纯数据节点的工作流。

【依赖方向】`model` ← `catalog` / `loader` ← `validator` ← 调用方（`engine` 在 P1）
        单向无环，`model` 只依赖标准库。
"""
from __future__ import annotations

__all__ = ["catalog", "loader", "model", "validator"]
