# -*- coding: utf-8 -*-
"""检查文件里"已导入但从未使用"的名字（项目未配 linter，此脚本补位）。

用法:
    python tests/check_imports.py                      # 默认扫 core/ 与根目录主要模块
    python tests/check_imports.py core/rag_engine.py   # 指定文件

判据：统计每个 imported name 在**非 import 语句**中的出现次数
（含被导入模块名用于属性访问，如 `json.dumps`）。
`__all__` 中列出的名字视为有意**再导出**，不计为未使用。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 目录会被递归展开（见 collect_files）。此处**逐个列出**而非直接写 ["core", ...]，
# 是因为整目录扫描会翻出两处**历史遗留**的未使用导入：
#   · core/config_manager.py:8   from pathlib import Path
#   · core/knowledge_base.py:19  import os
# 清理掉那两行之后，把这里改成 ["core", "tests"] 即可获得"新文件自动纳入"的覆盖。
DEFAULT_TARGETS = [
    # 核心模块
    "core/rag_engine.py",
    "core/memory_scope.py",               # 作用域 / 可写性判据
    "core/procedural_memory.py",          # 技能结晶档
    "core/skill_loader.py",               # 手写技能加载器
    "core/user_memory.py",                # L4 容器双库读写
    # Agent 运行时加固（docs/AGENT_RUNTIME.md）
    "core/llm_client.py",                 # LLM 客户端（含 token 用量采集）
    "core/prompt_layers.py",              # 提示分层与稳定前缀指纹
    "core/context_budget.py",             # 上下文双层预算
    "core/query_router.py",               # 分级网关
    "core/providers.py",                  # 模型服务商登记表
    "core/credentials.py",                # 只写凭据层
    "core/tools",                         # 工具层（含 capabilities 闸门）
    # 守卫与治理脚本（本轮新建的全部纳入 —— 名单不再"总在追新文件"）
    "tests/check_procedural_memory.py",
    "tests/check_skill_wiring.py",
    "tests/check_skill_loader.py",
    "tests/check_memory_split.py",
    "tests/check_architecture.py",
    "tests/check_prompt_layers.py",
    "tests/check_context_budget.py",
    "tests/check_query_router.py",
    "tests/check_tool_gate.py",
    "tests/check_tool_dispatch.py",
    "tests/check_trajectory_export.py",
    "core/trajectory.py",
    "tools/export_trajectory.py",
    "core/tools/dispatch.py",
    "tests/check_providers.py",
    "tests/check_credentials.py",
    "tests/check_providers_ui.py",
    "tests/check_eval_metrics.py",
    # 编排层（P0）
    "core/orchestration",
    "tests/check_workflow_contracts.py",
    "tests/check_workflow_validate.py",
    "tests/check_workflow_engine.py",
    "tests/check_workflow_checkpoint.py",
    "tests/check_workflow_gates.py",
    "tests/check_workflow_llm.py",
    "tests/check_workflow_web.py",
    "tests/check_workflow_review.py",
    "tests/check_uncertainty.py",
    "core/uncertainty.py",
    "tests/check_handoff.py",
    "core/handoff.py",
    "core/perception/contract.py",
    "core/perception/adapter.py",
    "core/perception/pipeline.py",
    "core/perception/report.py",
    "core/perception/intent.py",
    "core/task_runner.py",
    "tests/check_perception.py",
    "core/perception/demo.py",
    "core/perception/launch.py",
    "tests/check_ui_attrs.py",
    "tests/check_kb_freshness.py",
    "tests/check_error_injection.py",
    "tests/eval_error_injection.py",
    "tests/eval_tools.py",
    "ui/provider_editor.py",
    "tests/eval_judge_agreement.py",
    "tests/eval_procedural.py",
    "tests/eval_generation.py",
    "tests/eval_scenarios.py",
    "tests/harness.py",
    "tests/classify_l4.py",
    "tests/migrate_l4_split.py",
    "tests/prune_l4_source.py",
    "tests/backfill_and_clean_l4.py",
]


def collect_files(args: list[str]) -> list[Path]:
    files: list[Path] = []
    for a in args:
        p = (ROOT / a) if not Path(a).is_absolute() else Path(a)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
        elif p.is_file():
            files.append(p)
    return files


def check(path: Path) -> tuple[int, list[tuple[str, int]]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":      # 编译器指令，不是真导入
                continue
            for a in node.names:
                imported[a.asname or a.name] = node.lineno

    # __all__ 里显式声明的名字 = 有意再导出
    exported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    try:
                        exported |= set(ast.literal_eval(node.value))
                    except (ValueError, SyntaxError):
                        pass

    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            n = node
            while isinstance(n, ast.Attribute):
                n = n.value
            if isinstance(n, ast.Name):
                used.add(n.id)

    # 注解位置的前向引用字符串（如 `-> "RAGEngine"`）算作使用。
    # 注意：**不能**扫描任意字符串常量——docstring 里提到模块名会造成假阴性。
    for node in ast.walk(tree):
        for attr in ("annotation", "returns"):
            ann = getattr(node, attr, None)
            if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
                for nm in imported:
                    if nm in ann.value:
                        used.add(nm)

    unused = sorted(n for n in imported if n not in used and n not in exported)
    return len(imported), [(n, imported[n]) for n in unused]


def main() -> int:
    targets = sys.argv[1:] or DEFAULT_TARGETS
    files = collect_files(targets)
    if not files:
        print("[FAIL] 未匹配到任何文件")
        return 1

    total_unused = 0
    print("=" * 66)
    print(f"未使用导入检查 —— {len(files)} 个文件")
    print("=" * 66)
    for f in files:
        n_imp, unused = check(f)
        rel = f.relative_to(ROOT).as_posix()
        if unused:
            total_unused += len(unused)
            print(f"\n  [WARN] {rel}  未使用 {len(unused)}/{n_imp}")
            for name, ln in unused:
                print(f"           · {name}  (line {ln})")
        else:
            print(f"  [OK  ] {rel}  ({n_imp} 个导入，全部在用)")
    print()
    print(f"合计未使用: {total_unused}")
    return 1 if total_unused else 0


if __name__ == "__main__":
    raise SystemExit(main())
