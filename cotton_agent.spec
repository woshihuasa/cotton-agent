# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（--onedir）。

构建入口: python build_exe.py（自动注入版本号并调用本 spec）
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(__file__).resolve().parent if "__file__" in globals() else Path(SPECPATH)

# 主程序
a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 只读资产（知识库文档 + 图标 + 版本号），Windows 用分号分隔
        (str(ROOT / "data"), "data"),
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
    ]
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


