# -*- coding: utf-8 -*-
"""发布前密钥泄露自检（零 LLM / 零 API，可进 CI 守卫体系）。

为什么需要它（v0.3.3 实录）：
    v0.3.3 给 `config.app_data_dir()` 加了「%APPDATA% 不可写就降级到 exe 同级
    `runtime_data/`」的兜底 —— 这是让被安全软件拦截的机器能跑起来的必要修复。
    但它有个副作用：**只要有人在 `dist/CottonAgent/` 里试跑过一次应用，真 `.env`
    就会落到打包目录里**；此时直接压缩发布，用户密钥就跟着出去了。
    v0.3.3 开发过程中实测发生过：`dist/CottonAgent/runtime_data/.env` 里含全部 8 个键。

判据（两条都做，缺一不可）：
    1) **名字判据**：发布树里不允许出现 `.env`、`runtime_data/`、`data-gym-cache/`、
       `*.log` 等运行期产物；
    2) **内容判据**：把每个真实密钥值当字节串，在发布树的**每一个文件**里搜索，
       命中即失败。只有名字判据会漏掉"被写进 exe / 日志 / JSON 的值"。

用法:
    python tests/check_secrets.py                        # 默认扫 dist/ 下每个子目录
    python tests/check_secrets.py dist/CottonAgent-v0.3.2  # 指定目录（发布闸用法）

本脚本**绝不打印密钥值本身**，只打印键名与掩码前缀。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 密钥来源：这些文件里的真实值会被当作搜索目标
ENV_SOURCES = (
    ROOT / ".env",
    Path(os.environ.get("APPDATA") or "") / "CottonAgent" / ".env",
    Path(os.environ.get("LOCALAPPDATA") or "") / "CottonAgent" / ".env",
)

# 只把看起来像凭据的键当目标（URL / 模型名这类会大量误报）
_SECRET_NAME = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)

# 名字判据：这些名字出现在发布树里就是问题
FORBIDDEN_NAMES = {".env", ".env.local", "runtime_data", "data-gym-cache",
                   "startup_error.log", "faulthandler.log", "cotton_app.log"}
FORBIDDEN_SUFFIXES = (".log",)

# ★★ `_internal/` 是 PyInstaller 的**资产目录** —— 内容由 `spec` 的 `datas` 决定，
#    与发布目录顶层的"运行期产物"是两回事。
#
#    2026-10-20 修：`data-gym-cache` 是**双身份**同名 ——
#      · 顶层出现 = langchain 数据加载器写在 cwd 的缓存 → 该拦
#      · `_internal/` 里出现 = `spec` **主动打包**的 tiktoken 词表 → 必须放行
#    一刀切导致：`build_release` 的 `junk_filter` 修好之后，
#    本自检又把 `_internal/data-gym-cache/` 判成运行期产物 → 构建中止。
#    **两侧规则必须同步**，否则修复会在另一侧被拦下。
ASSET_DIR = "_internal"
ASSET_ALLOWED = {"data-gym-cache"}


def is_allowed_asset(rel: str, name: str) -> bool:
    """`_internal/<ASSET_ALLOWED>` 属于打包资产，不算运行期产物。"""
    return (name in ASSET_ALLOWED
            and ASSET_DIR in Path(rel).parts)

# 值短于这个长度就不适合当搜索目标（误报太多，也没实际泄露价值）
MIN_SECRET_LEN = 16


def mask(value: str) -> str:
    """够识别是哪个键，不足以构成泄露。"""
    return f"{value[:4]}…({len(value)} 字符)"


def load_secret_values() -> dict[str, str]:
    """收集 {真实值: 键名}。来源 = .env 文件 + 进程环境变量。"""
    found: dict[str, str] = {}

    def add(name: str, value: str) -> None:
        value = value.strip().strip('"').strip("'")
        if len(value) >= MIN_SECRET_LEN and _SECRET_NAME.search(name):
            found.setdefault(value, name)

    for path in ENV_SOURCES:
        try:
            if not path.is_file():
                continue
            for line in path.read_text("utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                add(name.strip(), value)
        except OSError:
            continue

    for name, value in os.environ.items():      # CI 里可能用环境变量传密钥
        add(name, value)

    return found


def scan_tree(tree: Path, needles: list[tuple[str, bytes]]) -> tuple[list[str], list[str], int]:
    """返回 (名字命中, 内容命中, 扫描文件数)。"""
    name_hits: list[str] = []
    content_hits: list[str] = []
    scanned = 0
    for root, dirs, files in os.walk(tree):
        for name in list(dirs):
            rel = str(Path(root, name).relative_to(tree).as_posix())
            if name in FORBIDDEN_NAMES and not is_allowed_asset(rel, name):
                name_hits.append(rel + "/")
        for name in files:
            rel = Path(root, name).relative_to(tree).as_posix()
            if (name in FORBIDDEN_NAMES and not is_allowed_asset(rel, name)) \
                    or name.endswith(FORBIDDEN_SUFFIXES):
                name_hits.append(rel)
            try:
                data = Path(root, name).read_bytes()
            except OSError:
                continue
            scanned += 1
            for key_name, needle in needles:
                if needle in data:
                    content_hits.append(f"{rel}  ←  {key_name}")
    return name_hits, content_hits, scanned


def default_targets() -> list[Path]:
    """默认扫描 `release/` 下**可发布**的子目录。

    跳过名为 `build` 的目录：那是 PyInstaller 的 --workpath（TOC/PYZ/警告文件等
    中间产物），永远不会被打包分发；扫它既浪费 148MB 的 I/O，也语义不对。
    历史兼容：早期产物在根目录 `dist/`，若存在也一并扫描。
    """
    targets: list[Path] = []
    for base in (ROOT / "release", ROOT / "dist"):
        if not base.is_dir():
            continue
        subdirs = [p for p in sorted(base.iterdir())
                   if p.is_dir() and p.name != "build"]
        targets.extend(subdirs or [base])
    return targets


def main() -> int:
    args = sys.argv[1:]
    targets = [Path(a) if Path(a).is_absolute() else (ROOT / a) for a in args] or default_targets()

    secrets = load_secret_values()
    print("=" * 72)
    print("发布前密钥泄露自检")
    print("=" * 72)
    print(f"扫描目标: {len(targets)} 个")
    for t in targets:
        print(f"  · {t}")
    print(f"搜索目标: {len(secrets)} 个密钥值")
    for value, name in secrets.items():
        print(f"  · {name:<24} {mask(value)}")
    if not secrets:
        print("  [WARN] 没找到任何密钥值 —— 只能做名字判据，内容判据失效")
        print("         请确认 .env 存在，或本机确实没有配置任何密钥。")

    needles = [(name, value.encode()) for value, name in secrets.items()]
    failed = False
    total_scanned = 0
    for tree in targets:
        print("-" * 72)
        print(f"[检查] {tree}")
        if not tree.is_dir():
            print("        [SKIP] 不是目录")
            continue
        name_hits, content_hits, scanned = scan_tree(tree, needles)
        total_scanned += scanned
        if name_hits:
            failed = True
            print(f"        [FAIL] 不允许出现的运行期产物 {len(name_hits)} 项:")
            for hit in name_hits[:20]:
                print(f"               · {hit}")
        else:
            print("        [OK  ] 名字判据：无 .env / runtime_data / *.log")
        if content_hits:
            failed = True
            print(f"        [FAIL] 文件内容里命中真实密钥 {len(content_hits)} 处:")
            for hit in content_hits[:20]:
                print(f"               · {hit}")
        else:
            print(f"        [OK  ] 内容判据：{scanned} 个文件里无密钥字节")

    print("=" * 72)
    print(f"合计扫描 {total_scanned} 个文件")
    if failed:
        print("[FAIL] 发布树里有密钥或运行期产物 —— **不要发布**。")
        print("       修法：删除 runtime_data/ 等运行期目录后重跑；发布包请用")
        print("       `python build_release.py` 生成（它会先剔除再自检）。")
        return 1
    print("[OK  ] 发布树干净，可以压缩发布。")
    print("       提醒：发布说明里必须写明「解压到可写目录」（不要放 C:\\Program Files）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())