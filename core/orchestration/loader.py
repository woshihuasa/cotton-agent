# -*- coding: utf-8 -*-
"""工作流加载器 —— YAML → `model.Workflow`。

【只做"翻译"，不做"判断"】本模块**不校验**节点类型是否存在、有没有环 ——
        那是 `validator.py` 的职责。分开的好处：
          · 加载错误（YAML 语法、字段类型）报得准；
          · 校验错误（语义问题）能**一次列出全部**，而不是撞到第一个就停。
        `load_workflow()` 会先加载、再校验，返回 `(workflow, issues)`。

【YAML 依赖】用 PyYAML（`.venv` 里已有 —— `config.py` 也间接用到）。
        刻意**不用** JSON：流程定义要能写注释、能 diff，YAML 明显更合适。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.orchestration.model import Node, ParamSpec, SideEffect, Workflow

#: 允许写进 YAML 的顶层键（多写会被校验器拒绝 —— 防拼写错误被静默忽略）。
ALLOWED_TOP_KEYS: frozenset[str] = frozenset({
    "id", "version", "description", "inputs", "nodes", "stages",
})

#: 允许写进节点的键。
ALLOWED_NODE_KEYS: frozenset[str] = frozenset({
    "id", "type", "params", "side_effect", "needs", "retry", "timeout",
    "timeout_s", "on_fail", "optional", "description", "on_reject",
})


class LoaderError(ValueError):
    """YAML 结构不合法（缺字段、类型错、拼写错）。"""


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _parse_side_effect(raw: Any, where: str) -> SideEffect | None:
    """`side_effect` 字段 → 枚举。**未写返回 None**（= 交给模型层的保守默认）。"""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    for se in SideEffect:
        if se.value == text:
            return se
    raise LoaderError(
        f"{where}: side_effect 只能是 "
        f"{[s.value for s in SideEffect]} 之一，收到 {raw!r}")


def _parse_params(raw: Any, where: str) -> dict[str, ParamSpec]:
    """`inputs:` 段 → `{名字: ParamSpec}`。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LoaderError(f"{where}: inputs 必须是映射（名字 → 声明），收到 {type(raw).__name__}")
    out: dict[str, ParamSpec] = {}
    for name, spec in raw.items():
        n = str(name).strip()
        if not n:
            raise LoaderError(f"{where}: inputs 里有空名字")
        if isinstance(spec, str):
            out[n] = ParamSpec(name=n, type=spec)
        elif isinstance(spec, dict):
            out[n] = ParamSpec(
                name=n,
                type=str(spec.get("type") or "string"),
                required=bool(spec.get("required", False)),
                default=spec.get("default"),
                description=str(spec.get("description") or ""),
            )
        else:
            raise LoaderError(f"{where}: inputs[{n!r}] 必须是字符串或映射，收到 {type(spec).__name__}")
    return out


def _parse_node(raw: Any, where: str) -> Node:
    if not isinstance(raw, dict):
        raise LoaderError(f"{where}: 节点必须是映射，收到 {type(raw).__name__}")
    unknown = set(raw) - ALLOWED_NODE_KEYS
    if unknown:
        # ★ 拼写错误必须报出来 —— 静默忽略等于让"写了但没生效"混进生产
        raise LoaderError(
            f"{where}: 未知字段 {sorted(unknown)}；"
            f"允许: {sorted(ALLOWED_NODE_KEYS)}")

    node_id = str(raw.get("id") or "").strip()
    node_type = str(raw.get("type") or "").strip()
    if not node_id:
        raise LoaderError(f"{where}: 节点缺少 id")
    if not node_type:
        raise LoaderError(f"{where}[{node_id}]: 节点缺少 type")

    params = raw.get("params")
    if params is not None and not isinstance(params, dict):
        raise LoaderError(f"{where}[{node_id}]: params 必须是映射")

    needs_raw = raw.get("needs")
    if isinstance(needs_raw, str):
        needs = [needs_raw]
    else:
        needs = [str(x) for x in _as_list(needs_raw)]

    timeout = raw.get("timeout_s", raw.get("timeout"))

    return Node(
        id=node_id,
        type=node_type,
        params=dict(params or {}),
        side_effect=_parse_side_effect(raw.get("side_effect"), f"{where}[{node_id}]"),
        needs=needs,
        retry=int(raw.get("retry") or 0),
        timeout_s=(float(timeout) if timeout is not None else None),
        on_fail=str(raw.get("on_fail") or "fail_fast"),
        optional=bool(raw.get("optional", False)),
        description=str(raw.get("description") or ""),
    )


def _collect_nodes(raw_doc: dict, where: str) -> list[Node]:
    """展开 `nodes:` 与 `stages[].nodes:` 两种写法。

    ★ 支持 `stages` 是**为了让文档里那份示意 YAML 能被真加载** ——
    但那只是"分组写法"，展开后就是一张平铺的节点表（编排层不关心分组）。
    """
    nodes: list[Node] = []
    for n in _as_list(raw_doc.get("nodes")):
        nodes.append(_parse_node(n, where))

    stages = raw_doc.get("stages")
    if stages is not None and not isinstance(stages, list):
        raise LoaderError(f"{where}: stages 必须是列表")
    for si, stage in enumerate(stages or []):
        if not isinstance(stage, dict):
            raise LoaderError(f"{where}: stages[{si}] 必须是映射")
        stage_id = str(stage.get("id") or f"stage{si}")
        for n in _as_list(stage.get("nodes")):
            nodes.append(_parse_node(n, f"{where}#{stage_id}"))
    return nodes


def load_dict(raw_doc: Any, *, source: str = "<dict>") -> Workflow:
    """从已解析的 dict 构造 `Workflow`（**不做语义校验**）。"""
    if not isinstance(raw_doc, dict):
        raise LoaderError(f"{source}: 顶层必须是映射，收到 {type(raw_doc).__name__}")
    unknown = set(raw_doc) - ALLOWED_TOP_KEYS
    if unknown:
        raise LoaderError(
            f"{source}: 未知顶层字段 {sorted(unknown)}；允许: {sorted(ALLOWED_TOP_KEYS)}")

    wf_id = str(raw_doc.get("id") or "").strip()
    if not wf_id:
        raise LoaderError(f"{source}: 缺少顶层 id")

    nodes = _collect_nodes(raw_doc, source)
    if not nodes:
        raise LoaderError(f"{source}: 没有任何节点")

    return Workflow(
        id=wf_id,
        version=int(raw_doc.get("version") or 1),
        nodes=nodes,
        inputs=_parse_params(raw_doc.get("inputs"), source),
        description=str(raw_doc.get("description") or ""),
        source=source,
    )


def load_text(text: str, *, source: str = "<text>") -> Workflow:
    """从 YAML 文本构造 `Workflow`。"""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LoaderError(f"{source}: YAML 解析失败: {e}") from e
    return load_dict(raw, source=source)


def load_file(path: str | Path) -> Workflow:
    """从文件加载工作流。"""
    p = Path(path)
    if not p.is_file():
        raise LoaderError(f"工作流文件不存在: {p}")
    return load_text(p.read_text(encoding="utf-8"), source=str(p))


def load_and_check(text_or_path, *, is_path: bool = False):
    """加载 + 静态校验。返回 `(workflow, issues)`；`issues` 为空表示通过。

    刻意**不抛异常**：调用方（守卫 / 界面）需要拿到**全部**问题一次看完。
    """
    from core.orchestration import validator

    if is_path:
        wf = load_file(text_or_path)
    else:
        wf = load_text(text_or_path)
    return wf, validator.validate(wf)


__all__ = [
    "ALLOWED_NODE_KEYS", "ALLOWED_TOP_KEYS", "LoaderError",
    "load_and_check", "load_dict", "load_file", "load_text",
]