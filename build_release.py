# -*- coding: utf-8 -*-
"""发布打包：构建 → 剔除运行期残留 → 签名 → 密钥自检 → 压缩。

和 `build_exe.py` 的分工：
    `build_exe.py`     —— 开发用，产出 `dist/CottonAgent/`（可以直接双击试跑）
    `build_release.py` —— 发布用，在此基础上做发布前必须做的几件事

**为什么必须单独有这一步（v0.3.3 实录）**：
    v0.3.3 起，应用在 `%APPDATA%` 不可写时会降级到 exe 同级的 `runtime_data/`
    （这是让被安全软件拦截的机器能跑起来的必要修复）。副作用是：**只要有人在
    `dist/CottonAgent/` 里试跑过应用，真 `.env`、会话、向量库就会落到那个目录里。**
    直接压缩就会把用户密钥一起发出去 —— v0.3.3 开发过程中实测发生过一次。
    所以发布包必须从**剔除过的副本**生成，而不是从 `dist/` 原地压缩。

用法:
    python build_release.py                 # 完整流程
    python build_release.py --skip-build    # 复用现有 dist/CottonAgent/，只做发布处理
    python build_release.py --no-sign       # 跳过签名（没有证书时）
    python build_release.py --no-updater    # 不构建 updater.exe

签名证书指纹默认取 v0.3.3 建的自签名证书，可用环境变量
`COTTON_SIGN_THUMBPRINT` 覆盖。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 父进程的输出会被子进程的直写穿插导致顺序错乱，改成行缓冲，日志更可读
sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import paths                                   # noqa: E402

DIST = paths.DIST_DIR                          # release/dist
SOURCE = paths.APP_DIR                         # release/dist/CottonAgent
RELEASE = paths.RELEASE_DIR                    # release/  ← 发布目录与 zip 都放这里
SIGNTOOL = Path(r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe")
DEFAULT_THUMBPRINT = "590009552AB76FC5F94FF52813A19D21558D0D21"
TIMESTAMP_URL = "http://timestamp.sectigo.com"   # digicert 的 http 已不可达、https 被旧版 signtool 拒

# 运行期产物：这些名字出现在**发布目录顶层**就剔除（`_internal/` 之内不动）。
# ⚠️ 注意 `data-gym-cache` 现在是**双身份**：
#    · 顶层出现时 = langchain 数据加载器写在 cwd 的缓存 → **剔除**
#    · `_internal/` 里出现时 = `spec` 打包的 tiktoken 词表 → **必须保留**
#    由 `junk_filter` 里的 `_internal` 判断区分，不要在这里删掉这条规则。
JUNK_NAMES = {
    "runtime_data",          # 降级数据目录 —— 可能含真 .env，最危险的一个
    "data-gym-cache",        # langchain 数据加载器在工作目录下的缓存
    "startup_error.log",
    "faulthandler.log",
    "cotton_app.log",
}

# `_internal/` 内**仍需剔除**的子项。
#
# ★ 为什么 `_internal/` 不能一律"不动"：`spec` 的 `datas` 是**整目录复制**的，
#   源码树里混着运行期状态时，它会被一起打进包。
#
# 键 = 相对于 `_internal/` 的父目录路径；值 = 要丢掉的名字。
ASSET_PRUNE: dict[str, set[str]] = {
    # `workflows/runs/` 是工作流**运行记录**：应用读的是
    # `app_data_dir()/workflows/runs`（见 `core/orchestration/store.py::runs_dir`），
    # **不会读包内这份**。分发它只是无用负载，还会把开发机的测试记录带给用户。
    "workflows": {"runs"},
}


def junk_filter(directory: str, names: list[str]) -> set[str]:
    """shutil.copytree 的 ignore 回调：剔除运行期产物，但保住打包资产。

    规则分两层：

    | 位置 | 处理 |
    |---|---|
    | **发布目录顶层** | 剔除 `JUNK_NAMES` / `*.log` / `tmpXXXXXXXX` |
    | **`_internal/` 之内** | **默认不动**（内容由 `spec` 的 `datas` 决定），但按 `ASSET_PRUNE` 剔除"混进资产的运行期状态" |

    ★★ 2026-10-20 修：docstring 一直写着"不动 `_internal`"，但代码**没做到** ——
      `skip` 在每一层都生效，于是 `_internal/data-gym-cache/` 被 `JUNK_NAMES`
      命中并剔除。后果：`spec` 明明把 tiktoken 词表打进了包（`dist/` 里确实有），
      复制到发布目录时又被删 —— **打包版离线分词依然会去 OpenAI 下载**，
      修复静默失效。实测：`dist/_internal/data-gym-cache` 存在，
      `CottonAgent-v0.4.0/_internal/data-gym-cache` 不存在。

      `_internal/` 是 PyInstaller 的**资产目录** —— 打包配置说它该在，
      就不该在这一步被删；只有 `ASSET_PRUNE` 里**显式列出**的才丢。
    """
    parts = Path(directory).parts
    if "_internal" in parts:
        # 相对 `_internal/` 的路径，用于查 ASSET_PRUNE
        rel = "/".join(parts[parts.index("_internal") + 1:])
        drop = ASSET_PRUNE.get(rel)
        return {n for n in names if n in drop} if drop else set()

    skip = {n for n in names if n in JUNK_NAMES or n.endswith(".log")}
    # PyInstaller 偶尔在 COLLECT 阶段留下 tmpXXXXXXXX 目录
    if Path(directory) == SOURCE:
        skip |= {n for n in names if n.startswith("tmp") and len(n) <= 16}
    return skip


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def sign(path: Path, thumbprint: str) -> bool:
    """用自签名证书签名；失败只告警，不阻断发布。"""
    if not SIGNTOOL.is_file():
        print(f"  [WARN] 找不到 signtool: {SIGNTOOL}")
        return False
    try:
        run([str(SIGNTOOL), "sign", "/fd", "sha256", "/sha1", thumbprint,
             "/tr", TIMESTAMP_URL, "/td", "sha256", str(path)])
        return True
    except subprocess.CalledProcessError as exc:
        print(f"  [WARN] 签名失败（不阻断发布）: {exc}")
        return False


def write_readme(stage: Path, version: str) -> None:
    """生成安装说明 —— 里面那条「解压到可写目录」是 P0 级注意事项。"""
    text = f"""CottonAgent {version} — 安装说明
{'=' * 46}

一、怎么运行
    1. 把整个文件夹解压到**可写目录**，例如  D:\\CottonAgent
       ⚠️ 不要放在 C:\\Program Files\\ 下：那里通常不可写，
          应用会退而使用系统临时目录，重启后数据会丢失。
    2. 双击 CottonAgent.exe。

二、第一次运行要做什么
    应用**不预置任何 API Key**。首次启动后请打开「设置」，
    填入你自己的 Key（DeepSeek / 硅基流动 / 高德 / Tavily）。
    这些 Key 只保存在你自己的电脑上（%APPDATA%\\CottonAgent\\.env，
    或本目录下的 runtime_data\\.env），不会随程序分发。

三、数据放在哪
    优先：%APPDATA%\\CottonAgent\\        （更新程序时不会丢）
    兜底：本目录下的 runtime_data\\        （当上面那个位置不可写时）
    本程序附带的知识库文档在 _internal\\data\\，属于只读资源。

四、卸载
    删掉本文件夹即可。若想连配置与记忆一起清掉，
    再删除 %APPDATA%\\CottonAgent\\。

五、目录结构
    CottonAgent.exe      主程序
    _internal\\           运行库与只读资源（请勿删除或改名）
    updater.exe          自动更新器（若随包提供）
"""
    (stage / "安装说明.txt").write_text(text, encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并生成可发布的压缩包")
    parser.add_argument("--skip-build", action="store_true", help="复用现有 dist/CottonAgent/")
    parser.add_argument("--no-sign", action="store_true", help="跳过 exe 签名")
    parser.add_argument("--no-updater", action="store_true", help="不构建 updater.exe")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from build_exe import build_app, build_updater, git_version   # noqa: E402

    version = git_version()
    # ⚠️ git describe 的结果**本身就带 v**（如 `v0.3.2`），不要再补一个 ——
    # 否则会产出 `CottonAgent-vv0.3.2`（v0.3.3 实测踩过）。
    tag = version if version.startswith("v") else f"v{version}"
    stage = RELEASE / f"CottonAgent-{tag}"
    print("=" * 72)
    print(f"发布打包 CottonAgent {tag}")
    print("=" * 72)

    # 1) 构建
    if args.skip_build:
        print("[1/6] 跳过构建（复用现有 dist/CottonAgent/）")
    else:
        print("[1/6] 构建主程序")
        build_app(version)
        if not args.no_updater:
            build_updater()
    if not SOURCE.is_dir():
        print(f"[FAIL] 找不到构建产物 {SOURCE} —— 先跑一次 build_exe.py")
        return 1

    # 2) 剔除运行期产物后复制到发布目录
    print(f"[2/6] 剔除运行期残留并复制到 {stage.name}/")
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(SOURCE, stage, ignore=junk_filter, symlinks=True)
    removed = sorted(set(p.name for p in SOURCE.iterdir()) -
                     set(p.name for p in stage.iterdir()))
    print(f"      已剔除: {', '.join(removed) if removed else '（无）'}")

    # 3) 安装说明
    print("[3/6] 写入 安装说明.txt")
    write_readme(stage, tag)

    # 4) 签名
    if args.no_sign:
        print("[4/6] 跳过签名")
    else:
        print("[4/6] 签名")
        thumb = os.environ.get("COTTON_SIGN_THUMBPRINT", DEFAULT_THUMBPRINT)
        sign(stage / "CottonAgent.exe", thumb)
        if (stage / "updater.exe").is_file():
            sign(stage / "updater.exe", thumb)

    # 5) 密钥自检 —— 不过就不压缩
    print("[5/6] 密钥泄露自检")
    guard = ROOT / "tests" / "check_secrets.py"
    result = subprocess.run([sys.executable, str(guard), str(stage)])
    if result.returncode != 0:
        print("[FAIL] 自检未通过，**已中止压缩**。请检查上面的命中项。")
        return 1

    # 6) 压缩
    print("[6/6] 压缩")
    archive = shutil.make_archive(str(RELEASE / stage.name), "zip", root_dir=RELEASE,
                                 base_dir=stage.name)
    size_mb = Path(archive).stat().st_size / 1048576
    print("=" * 72)
    print(f"[OK] 发布包: {archive}  ({size_mb:.1f} MB)")
    print(f"     目录:   {stage}")
    print("     发布前请再确认：发布说明里写了「解压到可写目录」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())