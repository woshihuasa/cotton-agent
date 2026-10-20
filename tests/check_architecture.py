# -*- coding: utf-8 -*-
"""阶段一结构守卫 —— 索引迁移 / 挂载配置 / 只读门禁 / 打包白名单。

【定位】`docs/ARCHITECTURE.md` §7「阶段一」的四步，各有一条可失败的不变量。
        本脚本把四条**纪律**变成**检查** —— 因为一句 `if` 或一条约定，
        不配上守卫就与本项目反复批评的"纸面约束"没有区别（ROADMAP 已把
        "阈值不是门禁"列为产品缺陷）。

【零 LLM / 零第三方依赖之外的部分】只有第 1 节会 import config（本地路径解析，
        不联网）；其余全部是纯函数与临时目录操作。

【覆盖】
        1. 路径迁移：结晶档默认路径 = `app_data_dir()/procedural_memory/index.jsonl`
        2. 配置与目录基建：挂载点解析 + 建目录 **且不产生任何注册表/索引文件**
        3. 只读门禁：`is_under`（含大小写不敏感）+ 向挂载点开可写存储**必须被拒**
        4. 打包白名单：**AST 抽取 spec 里真实的 `skills_datas()` 执行**（不是照抄逻辑），
           断言白名单收录、非白名单排除

【用法】python tests/check_architecture.py
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.memory_scope import is_read_only_mount, is_under      # noqa: E402
from core.procedural_memory import (                            # noqa: E402
    INDEX_NAME,
    RUNTIME_SUBDIR,
    ProceduralMemoryStore,
)

SANDBOX = ROOT / "tests" / "eval_outputs" / "architecture_sandbox"


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def main() -> int:
    c = Checker()
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    c.section("第 1 节 · 步 1：结晶档路径迁移")
    # ------------------------------------------------------------------
    c.check("常量 RUNTIME_SUBDIR / INDEX_NAME 就位",
            RUNTIME_SUBDIR == "procedural_memory" and INDEX_NAME == "index.jsonl",
            f"{RUNTIME_SUBDIR}/{INDEX_NAME}")

    default_store = ProceduralMemoryStore()          # 只读：缺文件不会创建
    p = default_store.store_path
    c.check("默认路径落在运行区目录下（…/procedural_memory/index.jsonl）",
            p.name == INDEX_NAME and p.parent.name == RUNTIME_SUBDIR, str(p))
    c.check("默认路径**不再是**旧的单文件 procedural_memory.jsonl",
            p.name != "procedural_memory.jsonl", str(p))

    # 沙箱路径与生产路径同构
    sb = ProceduralMemoryStore(SANDBOX / RUNTIME_SUBDIR / INDEX_NAME)
    c.check("沙箱作用域与生产路径同构",
            sb.store_path.parent.name == RUNTIME_SUBDIR, str(sb.store_path))
    sb.record_trajectory(["t_a"], triggers=["测试"])
    c.check("落盘会自动建出父目录（无需调用方 mkdir）", sb.store_path.exists())
    c.check("落盘后父目录名仍正确", sb.store_path.parent.name == RUNTIME_SUBDIR)

    # ------------------------------------------------------------------
    c.section("第 2 节 · 步 2：配置与目录基建（只建目录，不注册）")
    # ------------------------------------------------------------------
    import config                                              # noqa: E402

    shipped = config.shipped_skill_dir()
    c.check("shipped_skill_dir 解析到资源区（…/skills）",
            shipped.name == "skills", str(shipped))
    c.check("shipped_skill_dir 是绝对路径", shipped.is_absolute(), str(shipped))
    c.check("skill_mount_dirs 至少含随版本分发的 skills/",
            any(d == shipped for d in config.skill_mount_dirs()),
            str(config.skill_mount_dirs()))

    os.environ["EXTERNAL_SKILL_DIRS"] = os.pathsep.join(["C:/repos/skills-a", "D:/skills-b"])
    ext = config.external_skill_dirs()
    c.check("external_skill_dirs 解析多路径（os.pathsep 分隔）",
            [d.name for d in ext] == ["skills-a", "skills-b"], str(ext))
    c.check("挂载点清单＝随版本 + 外部挂载",
            len(config.skill_mount_dirs()) == 3, str(config.skill_mount_dirs()))
    os.environ.pop("EXTERNAL_SKILL_DIRS", None)
    c.check("未配置外部挂载时清单只含随版本目录",
            len(config.skill_mount_dirs()) == 1, str(config.skill_mount_dirs()))

    import bootstrap                                           # noqa: E402
    fresh = SANDBOX / "runtime_probe"
    created = bootstrap.ensure_runtime_dirs(fresh)
    c.check("ensure_runtime_dirs 建出两个目录", len(created) == 2, str(created))
    c.check("procedural_memory/ 存在", (fresh / "procedural_memory").is_dir())
    c.check("procedural_memory/quarantine/ 存在",
            (fresh / "procedural_memory" / "quarantine").is_dir())

    # ★ 核心不变量：只建目录、**不注册**（不写任何文件）
    stray = [f for f in (fresh / "procedural_memory").rglob("*") if f.is_file()]
    c.check("★ 建目录过程**不产生任何文件**（无注册表 / 无索引）",
            not stray, f"意外创建: {[str(x) for x in stray]}")
    c.check("quarantine 内也是空的（只有目录）",
            not any((fresh / "procedural_memory" / "quarantine").iterdir()))

    # ------------------------------------------------------------------
    c.section("第 3 节 · 步 3：只读挂载门禁")
    # ------------------------------------------------------------------
    mount = SANDBOX / "skills"
    (mount / "demo").mkdir(parents=True, exist_ok=True)
    runtime = SANDBOX / "runtime_zone"

    c.check("is_under：同路径为真", is_under(mount, mount))
    c.check("is_under：子路径为真", is_under(mount / "demo" / "x.md", mount))
    c.check("is_under：兄弟路径为假", not is_under(SANDBOX / "other", mount))
    c.check("is_under：**大小写不敏感**（Windows）",
            is_under(Path(str(mount).upper()), mount),
            f"{str(mount).upper()} vs {mount}")
    c.check("is_under：前缀相近但不是子目录（skills-2 vs skills）为假",
            not is_under(SANDBOX / "skills-2", mount))

    c.check("挂载点内 → is_read_only_mount 为真",
            is_read_only_mount(mount / "demo" / "x.md", [mount]))
    c.check("挂载点外 → 为假", not is_read_only_mount(runtime / INDEX_NAME, [mount]))
    c.check("无挂载点 → 恒为假", not is_read_only_mount(mount / "x.md", None))

    refused = False
    try:
        ProceduralMemoryStore(mount / INDEX_NAME, mount_dirs=[mount])
    except PermissionError:
        refused = True
    c.check("★ 向挂载点开可写存储 → **被拒**（PermissionError）", refused)

    allowed = True
    try:
        ProceduralMemoryStore(runtime / RUNTIME_SUBDIR / INDEX_NAME, mount_dirs=[mount])
    except PermissionError:
        allowed = False
    c.check("向运行区开可写存储 → 正常放行", allowed)
    c.check("未注入 mount_dirs 时不拦（保持零依赖可用）",
            ProceduralMemoryStore(mount / INDEX_NAME) is not None)

    # ------------------------------------------------------------------
    c.section("第 4 节 · 步 4：打包白名单（AST 抽取 spec 真实函数执行）")
    # ------------------------------------------------------------------
    spec_path = ROOT / "cotton_agent.spec"
    spec_src = spec_path.read_text("utf-8")
    tree = ast.parse(spec_src)

    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "skills_datas"), None)
    c.check("spec 中定义了 skills_datas()", fn is not None)
    c.check("spec 的 datas= 里调用了 skills_datas()",
            "skills_datas()" in spec_src)

    consts: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SKILL_SUFFIXES":
                    consts[t.id] = ast.literal_eval(node.value)
    c.check("抽取到 SKILL_SUFFIXES 白名单", "SKILL_SUFFIXES" in consts, str(consts))

    fake = SANDBOX / "fake_repo"
    (fake / "skills" / "demo" / "scripts").mkdir(parents=True, exist_ok=True)
    (fake / "skills" / "demo" / "SKILL.md").write_text("说明", encoding="utf-8")
    (fake / "skills" / "demo" / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (fake / "skills" / "notes.tmp").write_text("临时", encoding="utf-8")   # 非白名单
    (fake / "skills" / "shot.png").write_bytes(b"\x89PNG")                 # 非白名单

    if fn is not None:
        module = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(module)
        ns = {"ROOT": fake, "Path": Path, **consts}
        exec(compile(module, "<spec>", "exec"), ns)          # noqa: S102
        entries = ns["skills_datas"]()
        srcs = {Path(s).name for s, _ in entries}
        dsts = {d for _, d in entries}
        c.check("白名单内的文件被收录（SKILL.md + run.py）",
                srcs == {"SKILL.md", "run.py"}, str(srcs))
        c.check("★ 非白名单文件**不进包**（notes.tmp / shot.png）",
                "notes.tmp" not in srcs and "shot.png" not in srcs, str(srcs))
        c.check("目标目录保持 skills/ 下的相对结构",
                dsts == {str(Path("skills") / "demo"),
                         str(Path("skills") / "demo" / "scripts")}, str(dsts))

    # skills/ 缺失时必须优雅跳过（不抛异常）
    ns2 = {"ROOT": SANDBOX / "empty_repo", "Path": Path, **consts}
    module2 = ast.Module(body=[fn], type_ignores=[]) if fn else None
    if module2 is not None:
        ast.fix_missing_locations(module2)
        exec(compile(module2, "<spec>", "exec"), ns2)         # noqa: S102
        c.check("skills/ 不存在时返回空列表而不抛异常",
                ns2["skills_datas"]() == [])

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"阶段一结构守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())