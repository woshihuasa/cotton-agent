# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（--onedir）。

构建入口: python build_exe.py（自动注入版本号并调用本 spec）
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(__file__).resolve().parent if "__file__" in globals() else Path(SPECPATH)

# 知识库允许分发的文件类型。`data/` 本应只放这些（文档 + 统计数据），
# 但历史上曾有运行时产物落进去（如早期版本写图的 data/charts/*.png），
# 被整目录照搬进了 v0.2.1 / v0.3.0 发布包（ROADMAP 短板 #20）。
# 因此改为**白名单收集**：非白名单文件不进包，并打印出来提醒清理。
KB_SUFFIXES = {".md", ".csv", ".pdf", ".txt"}


def kb_datas():
    """按白名单收集 data/ 下的知识库文档，返回 PyInstaller 的 (源, 目标目录) 列表。

    显式逐文件枚举（而非整目录照搬），确保运行时产物永远进不了发布包。
    """
    data_root = ROOT / "data"
    entries, skipped = [], []
    for f in sorted(data_root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(data_root)
        if f.suffix.lower() in KB_SUFFIXES:
            entries.append((str(f), str(Path("data") / rel.parent)))
        else:
            skipped.append(rel.as_posix())
    if skipped:
        print(f"[spec] 已排除 {len(skipped)} 个非知识库文件（不随包分发，建议从 data/ 清理）:")
        for s in skipped[:20]:
            print(f"[spec]    data/{s}")
    print(f"[spec] 知识库文件 {len(entries)} 个将随包分发")
    return entries


# 主程序
a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 只读资产（知识库文档 + Web 页面 + 图标 + 版本号）
        *kb_datas(),
        (str(ROOT / "web"), "web"),
        (str(ROOT / "ui" / "icons"), "ui/icons"),
        (str(ROOT / "build_version.txt"), "."),
    ],
    hiddenimports=[
        # ChromaDB / LangChain 动态导入点
        "chromadb.api.segment",
        "chromadb.api.types",
        "chromadb.utils.embedding_functions",
        "chromadb_rust_bindings",
        "langchain_text_splitters",
        "langchain_community.document_loaders",
        "openai",
        "tiktoken_ext.openai_public",
        "tiktoken_ext",
        # Web 服务（uvicorn 会动态加载 loops / protocols 等子模块）
        "fastapi",
        "starlette",
    ]
    + collect_submodules("uvicorn")
    + collect_submodules("chromadb"),  # chromadb 1.x 大量动态导入（含 Rust 后端）,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "tkinter"],
    noarchive=False,
    optimize=0,
)

# 动态导入的第三方库全量收集（排除 __pycache__ 与 .pyc，避免复制损坏的字节码文件）
for pkg in [
    "langchain", "langchain_community", "langchain_chroma",
    "langchain_openai", "langchain_text_splitters", "chromadb",
    "chromadb_rust_bindings",
    "tiktoken", "openai", "tavily", "markdown", "pandas",
    "matplotlib", "numpy", "requests", "dotenv", "pymupdf",
    # Web 服务（uvicorn 内部动态导入较多，整包收集更稳）
    "fastapi", "starlette", "uvicorn", "pydantic",
]:
    try:
        a.datas += Tree(
            Path(__import__(pkg).__file__).parent, prefix=pkg.replace(".", "/"),
            excludes=["__pycache__", "*.pyc"],
        )
    except Exception:
        pass

# ChromaDB 原生扩展（.pyd/.dll）作为二进制收集
a.binaries += collect_dynamic_libs("chromadb") + collect_dynamic_libs("chromadb_rust_bindings")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CottonAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无控制台窗口
    icon=str(ROOT / "ui" / "icons" / "app.ico") if (ROOT / "ui" / "icons" / "app.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="CottonAgent",
)


