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
DEFAULT_TARGETS = ["core/rag_engine.py", "core/tools"]


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
