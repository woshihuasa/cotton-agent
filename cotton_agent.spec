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


# 手写技能允许分发的文件类型。技能目录**随版本分发**（真相源在 git），
# 但其中会有人的脚本与说明，故仍走**白名单逐文件收集** —— 与 kb_datas 同一做法
# （S7-A 的教训：整目录照搬会静默夹带运行时产物）。
SKILL_SUFFIXES = {".md", ".yaml", ".yml", ".json", ".py", ".sh", ".ps1", ".txt"}


def skills_datas():
    """按白名单收集 skills/ 下的手写技能，返回 PyInstaller 的 (源, 目标目录) 列表。

    运行时这些文件位于 `_MEIPASS/skills/`，由 `config.shipped_skill_dir()` 定位，
    并作为**只读挂载点**被读取（docs/ARCHITECTURE.md §3.1、§4）。
    """
    skills_root = ROOT / "skills"
    if not skills_root.exists():
        print("[spec] skills/ 不存在 —— 本次不收录手写技能")
        return []
    entries, skipped = [], []
    for f in sorted(skills_root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(skills_root)
        if f.suffix.lower() in SKILL_SUFFIXES:
            entries.append((str(f), str(Path("skills") / rel.parent)))
        else:
            skipped.append(rel.as_posix())
    if skipped:
        print(f"[spec] 已排除 {len(skipped)} 个非技能文件（不随包分发，建议从 skills/ 清理）:")
        for s in skipped[:20]:
            print(f"[spec]    skills/{s}")
    print(f"[spec] 手写技能文件 {len(entries)} 个将随包分发")
    return entries


# 主程序
a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 只读资产（知识库文档 + Web 页面 + 图标 + 版本号）
        *kb_datas(),
        *skills_datas(),
        (str(ROOT / "web"), "web"),
        (str(ROOT / "ui" / "icons"), "ui/icons"),
        (str(ROOT / "build_version.txt"), "."),
        # ★ 运行期资产，2026-10-20 补：
        #   `workflows/` 是编排层的**权威定义**（决策 3a）—— 没有它，
        #     `/perception` 报「工作流文件不存在」、`/workflow` 页面恒空。
        #   `prompts/` 是 `llm.decide` / `llm.summarize` 节点的**必填依赖**
        #     （`prompt_ref` 是 required 参数）—— 没有它，任何 llm 节点必失败。
        #   两者原先都不在 datas 里，打包版两个功能直接是坏的。
        (str(ROOT / "workflows"), "workflows"),
        (str(ROOT / "prompts"), "prompts"),
        # ★ tiktoken 的 BPE 词表（1.6 MB）。没有它，token 计数在**离线**环境
        #   会去 openaipublic.blob.core.windows.net 下载，无网即抛 SSLError。
        #   实测触发点：`core/rag_engine.py` 的 `tiktoken.get_encoding("cl100k_base")`。
        (str(ROOT / "data-gym-cache"), "data-gym-cache"),
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
    excludes=[
        "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "tkinter",
        # ⚠️ v0.3.3：以下大包被 transformers 间接拖入，但项目 RAG/rerank 只用
        # onnxruntime（不依赖 torch/scipy/sklearn）。hook-torch 在隔离子进程里
        # 收集 torch.testing._internal.optests 时触发 0xC0000005 段错误，直接
        # 中断打包；excludes 掉它们，打包从"必崩"变"快速通过"，包体也大幅缩小。
        "torch", "torchvision", "torchaudio",
        "transformers",
        "scipy", "sklearn", "scikit-learn",
        "sympy",  # torch/scipy 的间接依赖，同样不需要
        "tensorboard",
    ],
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



