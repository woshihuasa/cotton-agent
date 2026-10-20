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
import re
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

    # ==================================================================
    c.section("第 5 节 · 打包资源覆盖（2026-10-20 新增）")
    # ==================================================================
    # 【它守的那一类 bug】
    #   打包版里 `/perception` 报「工作流文件不存在」、`/workflow` 页面恒空 ——
    #   原因是 `workflows/` 与 `prompts/` **没进 spec 的 datas**，且代码用
    #   `Path("workflows/...")`（相对 cwd）/ `Path(__file__).parent.parent`
    #   推导资源路径 —— 打包后两者都指错。
    #
    # 【为什么第 4 节没抓到】
    #   第 4 节断言的是「spec 里已有的条目对不对」（白名单式），
    #   **没有断言「该有的条目有没有」**（覆盖式）。
    #   `workflows/` 从头到尾没进过任何人的清单。
    # ==================================================================

    from config import resource_path as _rp

    #: 运行期**必需**的打包资源（顶层路径）。
    #: ★ 新增资源目录时**必须**加到这里 —— 这是本节的契约。
    PKG_RESOURCES = (
        "data",              # 知识库文档（kb_datas 已覆盖）
        "skills",            # 技能库（skills_datas 已覆盖）
        "web",               # Web 页面
        "ui/icons",          # 图标
        "workflows",         # ★ 编排层权威定义（2026-10-20 补）
        "prompts",           # ★ llm 节点的 prompt_ref 依赖（2026-10-20 补）
        "build_version.txt",  # 版本号
    )

    # ① 每个资源在开发树上都存在
    for rel in PKG_RESOURCES:
        _p = ROOT / rel
        c.check(f"资源存在：{rel}", _p.exists(), str(_p))

    # ② spec 的 datas 覆盖每一个
    _spec_src = (ROOT / "cotton_agent.spec").read_text("utf-8")
    for rel in PKG_RESOURCES:
        top = rel.split("/")[0]
        # 允许两种写法：`(str(ROOT / "x"), "x")` 或 `*x_datas()`
        covered = (f'"{top}"' in _spec_src or f"{top}_datas()" in _spec_src)
        c.check(f"★ spec datas 覆盖：{rel}", covered,
                "datas 里找不到 " + top)

    # ③ `resource_path()` 能解析到真实位置
    for rel in PKG_RESOURCES:
        _p = _rp(rel)
        c.check(f"resource_path 可解析：{rel}", _p.exists(), str(_p))

    # ④ ★★★ 4 处曾经的 bug 点：必须都落在 `resource_path` 的基准下
    _base = _rp("")
    try:
        from core import web_server as _WS
        _wf_dir = _WS._workflows_dir()
        c.check("★★ web_server._workflows_dir() 在资源基准下",
                str(_wf_dir).startswith(str(_base)), str(_wf_dir))
        # ★★★ 从**源码**断言：`_workflows_dir` 里不许再出现 mkdir。
        #   原先那句 `d.mkdir(parents=True, exist_ok=True)` 是静默失败的制造者：
        #   资产缺失时会造出一个**空目录**，把错误从「文件不存在」
        #   降级成「列表为空」—— 后者看起来像"正常但没数据"。
        _ws_src = (ROOT / "core" / "web_server.py").read_text("utf-8")
        _ws_tree = ast.parse(_ws_src)
        _fn = next((n for n in ast.walk(_ws_tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_workflows_dir"), None)
        _mk = []
        if _fn is not None:
            for _n in ast.walk(_fn):
                if (isinstance(_n, ast.Call)
                        and isinstance(_n.func, ast.Attribute)
                        and _n.func.attr in ("mkdir", "makedirs")):
                    _mk.append(_n.func.attr)
        c.check("★★★ `_workflows_dir()` 里**没有** mkdir（不伪造空目录）",
                not _mk, str(_mk))
    except Exception as e:                                # noqa: BLE001
        c.check("web_server._workflows_dir() 可用", False,
                f"{type(e).__name__}: {e}")

    try:
        from core.perception import launch as _L
        _wf_path = _L._workflow_path()
        c.check("★★ perception._workflow_path() 在资源基准下",
                str(_wf_path).startswith(str(_base)), str(_wf_path))
        c.check("★★ 且该文件真实存在", _wf_path.is_file(), str(_wf_path))
    except Exception as e:                                # noqa: BLE001
        c.check("perception._workflow_path() 可用", False,
                f"{type(e).__name__}: {e}")

    # ⑤ ★★★ 静态检查：核心代码里**不许**用 `__file__` 推导资源路径
    #    这正是四处 bug 的共同模式。允许 `__file__` 用于**代码定位**
    #    （如 paths.ROOT），但资源路径必须走 resource_path。
    # ★★ 两种形式都要抓（第一版只抓字面量，漏了变量 —— 实测漏掉 prompt_ref）：
    #   ① `...parent.parent.parent / "workflows"`   ← 字面量
    #   ② `...parent.parent.parent / prompt_ref`    ← **变量**（资源路径存在参数里）
    #   判据：只要出现「`__file__` 推导链 + `/`」，右操作数是字面量资源名
    #   或**已知的资源引用参数名**，就算违规。
    _REF_PARAMS = (
        "prompt_ref", "rubric_ref", "workflow_path", "template_ref",
        "prompt_path", "rubric_path",
    )
    _BAD_LIT = re.compile(
        r"Path\(__file__\)\.resolve\(\)\.parent(?:\.parent)+\s*/\s*"
        r"[\"'](?:data|skills|web|ui|workflows|prompts)[\"']")
    _BAD_VAR = re.compile(
        r"Path\(__file__\)\.resolve\(\)\.parent(?:\.parent)+\s*/\s*("
        + "|".join(_REF_PARAMS) + r")\b")
    _offenders = []
    for _f in sorted((ROOT / "core").rglob("*.py")):
        _txt = _f.read_text("utf-8")
        for _ln, _line in enumerate(_txt.splitlines(), 1):
            if _BAD_LIT.search(_line):
                _offenders.append(f"{_f.name}:{_ln} 字面量")
            _mv = _BAD_VAR.search(_line)
            if _mv:
                _offenders.append(f"{_f.name}:{_ln} {_mv.group(1)}")
    c.check("★★★ 无 `__file__` 推导资源路径的残留（字面量 + 变量两种形式）",
            not _offenders, str(_offenders))

    # ⑥ ★★★ tiktoken 词表：必须**已缓存**且**已打包**
    #    踩过的坑：`tiktoken.get_encoding("cl100k_base")` 找不到缓存时会去
    #    `openaipublic.blob.core.windows.net` 下载。默认查找位置是
    #    `tempfile.gettempdir()/data-gym-cache`，而本机 `%TEMP%` 不可写 →
    #    `gettempdir()` 退化到 cwd 且结果被缓存 → 目录随 cwd 变。
    #    实测后果：打包版**离线必失败**；守卫 `check_handoff` 4 次里错 3 次。
    import os as _os

    import bootstrap as _bs

    c.check("★★★ TIKTOKEN_CACHE_DIR 已被钉住（不依赖 cwd）",
            bool(_os.environ.get("TIKTOKEN_CACHE_DIR")),
            "未设置 —— cwd 一变缓存就找不到")
    c.check("★★★ tiktoken 词表已缓存（运行期无需联网）",
            _bs.tiktoken_cache_ready(),
            _os.environ.get("TIKTOKEN_CACHE_DIR", ""))
    c.check("★★ spec datas 打包了 data-gym-cache",
            '"data-gym-cache"' in _spec_src,
            "打包版离线会抛 SSLError")

    # ⑥ ★ 反向验证：覆盖检查器**真能发现缺项**（不是空转）
    def _missing(datas_text: str) -> list[str]:
        return [r for r in PKG_RESOURCES
                if not (f'"{r.split("/")[0]}"' in datas_text
                        or f"{r.split('/')[0]}_datas()" in datas_text)]

    c.check("★ 覆盖检查器：真实 spec 无缺项", not _missing(_spec_src),
            str(_missing(_spec_src)))
    _broken = _spec_src.replace('(str(ROOT / "workflows"), "workflows"),', "")
    c.check("★★★ 覆盖检查器能发现被删掉的 workflows",
            "workflows" in _missing(_broken), str(_missing(_broken)))
    _broken2 = _spec_src.replace('(str(ROOT / "prompts"), "prompts"),', "")
    c.check("★★★ 覆盖检查器能发现被删掉的 prompts",
            "prompts" in _missing(_broken2), str(_missing(_broken2)))

    # ==================================================================
    c.section("第 6 节 · 发布资产过滤（`build_release.junk_filter`）")
    # ==================================================================
    # 【它守的那一类 bug —— 2026-10-20 实际踩到】
    #   `junk_filter` 的 docstring 写着"不动 `_internal`"，但代码在每一层都过滤，
    #   于是 `_internal/data-gym-cache/`（spec 主动打包的 tiktoken 词表）
    #   被 `JUNK_NAMES` 命中删掉 —— **打包版离线分词修复静默失效**。
    #   而"静默"二字是关键：构建照样成功，包照样产出，只有真去用才发现。
    #
    # 【为什么这节要断言"两侧同步"】
    #   同一份名字规则同时存在于 `build_release.JUNK_NAMES` 与
    #   `tests/check_secrets.FORBIDDEN_NAMES`。修了一侧、另一侧没跟，
    #   修复就会在另一侧被拦下（实测发生过：构建中止在自检那步）。
    # ==================================================================

    try:
        import build_release as _br

        _SRC = str(_br.SOURCE)
        _INT = str(Path(_SRC) / "_internal")
        _names = ["data-gym-cache", "runtime_data", "x.log", "tmpabc123",
                  "workflows", "runs", "prompts", "web"]

        c.check("★★★ 顶层剔除运行期产物（junk / log / tmp）",
                _br.junk_filter(_SRC, _names)
                == {"data-gym-cache", "runtime_data", "x.log", "tmpabc123"},
                str(sorted(_br.junk_filter(_SRC, _names))))
        c.check("★★★ `_internal/` 顶层**不剔除**任何东西（资产由 spec 决定）",
                _br.junk_filter(_INT, _names) == set(),
                str(sorted(_br.junk_filter(_INT, _names))))
        c.check("★★★ `_internal/workflows/` 剔除 `runs/`（运行期记录，无用负载）",
                _br.junk_filter(str(Path(_INT) / "workflows"), _names) == {"runs"},
                str(sorted(_br.junk_filter(str(Path(_INT) / "workflows"), _names))))

        # ① ★★★ 反向约束：ASSET_PRUNE 不许误删 spec 声明要分发的资产
        _asset_names = set(re.findall(r'\(str\(ROOT / "[^"]+"\), "([^"]+)"\)', _spec_src))
        _pruned = set()
        for _parent, _drops in _br.ASSET_PRUNE.items():
            _pruned |= _drops
        c.check("★★★ ASSET_PRUNE 不含 spec 的顶层资产（不误删该分发的）",
                not (_asset_names & _pruned), str(sorted(_asset_names & _pruned)))
        c.check("★ 且 spec 资产在 `_internal` 下确实不被整目录剔除",
                all(_br.junk_filter(_INT, [_a]) == set() for _a in _asset_names),
                str(sorted(_asset_names)))

        # ② 与 check_secrets 的规则同步（两侧同名规则必须一致）
        import importlib.util as _ilu
        _cs_spec = _ilu.spec_from_file_location(
            "_cs_probe", ROOT / "tests" / "check_secrets.py")
        _cs = _ilu.module_from_spec(_cs_spec)
        _cs_spec.loader.exec_module(_cs)
        c.check("★★★ `_internal/data-gym-cache` 两侧都放行（junk_filter + check_secrets）",
                _br.junk_filter(_INT, ["data-gym-cache"]) == set()
                and _cs.is_allowed_asset("_internal/data-gym-cache", "data-gym-cache"))
        c.check("★★★ `_internal/runtime_data` 两侧都**不放行**（危险项不许开口子）",
                _cs.is_allowed_asset("_internal/runtime_data", "runtime_data") is False
                and "runtime_data" in _br.JUNK_NAMES)
        c.check("★★ 顶层 `data-gym-cache` 两侧都仍被拦（缓存身份不放过）",
                "data-gym-cache" in _br.junk_filter(_SRC, ["data-gym-cache"])
                and _cs.is_allowed_asset("data-gym-cache", "data-gym-cache") is False)

    except Exception as e:                                # noqa: BLE001
        c.check("发布资产过滤检查跑通", False, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"阶段一结构守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())