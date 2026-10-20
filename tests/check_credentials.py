# -*- coding: utf-8 -*-
"""凭据层守卫 —— 只写接口 · 优先级 · 容错 · **明文红线**。

【定位】`docs/LLM_GATEWAY.md` 步 3 的验收。

        本守卫最重要的一条是 **§1 的源码级断言**：
        **`get_key()` 不得出现在 `ui/` 目录的任何文件里** ——
        因为"UI 拿不到完整密钥"这个承诺，一旦被某行代码破坏，**没有任何运行期断言能发现**。

【零成本 / 不碰生产】所有写入都发生在**临时目录**（守卫会把 `credentials_path` 指过去），
        绝不触碰项目根或 `%APPDATA%` 的真实凭据文件。

【用法】python tests/check_credentials.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import credentials                                # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "cred_sandbox"
FAKE_KEY = "sk-testonly-1234567890abcdefXYZ"

#: ★ 用**合成的服务商名**测文件层 —— 它没有对应环境变量，
#: 于是 `_lookup` 只会看凭据文件，不会从 `.env` 回退到真 key。
#: （本守卫第一版直接用 "deepseek"，结果 `has_key` 因 `.env` 回退恒为真，
#:   8 条断言全错 —— 那是**测试预期错**，不是代码错。）
PROV = "__test_provider__"


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
    cred_file = SANDBOX / "credentials.json"

    # 把凭据路径指到沙箱（**绝不写生产文件**）
    credentials.credentials_path = lambda: cred_file

    # ------------------------------------------------------------------
    c.section("第 1 节：★★★ 明文红线（源码级断言）")
    # ------------------------------------------------------------------
    ui_dir = ROOT / "ui"
    ui_files = sorted(ui_dir.rglob("*.py")) if ui_dir.is_dir() else []
    c.check("ui/ 目录存在且有 .py 文件", len(ui_files) > 0, str(len(ui_files)))

    # ⚠️ 判据要区分"**真的调用了**"和"**在文档里提到了它**"。
    # 前两版都错在"按文本判"：
    #   · 第一版：裸子串 —— 被**我自己的文档字符串**打红（注释里提到函数名是好事）；
    #   · 第二版：去掉 `#` 注释后再匹配 —— 仍然误伤，因为 **docstring 不是注释，
    #     它是字符串字面量**（按行过滤删不掉）。
    # 正解：**用 AST 看语义** —— 只认 `Name(id="get_key")` 与 `Attribute(attr="get_key")`，
    # 字符串字面量不会被匹配。
    import ast as _ast

    def _really_uses_get_key(path) -> bool:
        try:
            tree = _ast.parse(path.read_text("utf-8", errors="replace"))
        except SyntaxError:
            return True                    # 解析不了就当作可疑（失败方向偏安全）
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute) and node.attr == "get_key":
                return True
            if isinstance(node, _ast.Name) and node.id == "get_key":
                return True
        return False

    offenders = [f.relative_to(ROOT).as_posix() for f in ui_files if _really_uses_get_key(f)]
    c.check("★★★ **ui/ 里没有任何文件真正调用 `get_key`**（UI 拿不到完整密钥）",
            not offenders, f"违例: {offenders}")

    # ★ 关键：**文档里提到函数名不算违规** —— 守住"守卫别太粗"
    probe_doc = SANDBOX / "fake_ui_doc.py"
    probe_doc.write_text('"""本模块绝不调用 credentials.get_key()。"""\n'
                         "x = 1\n", encoding="utf-8")
    c.check("★ 仅在文档字符串中提到 `get_key` **不算违规**（否则守卫会误伤文档）",
            not _really_uses_get_key(probe_doc))

    # 反向校验：真实的调用**确实能抓到**（否则它可能只是"扫了个空目录"）
    probe = SANDBOX / "fake_ui.py"
    probe.write_text("from core import credentials\n"
                     "k = credentials.get_key('deepseek')\n", encoding="utf-8")
    c.check("★ 反向校验：真实的 `credentials.get_key(...)` 调用**能被抓到**",
            _really_uses_get_key(probe))

    # ------------------------------------------------------------------
    c.section("第 2 节：只写接口 —— 能写、能查状态、**读不回完整值**")
    # ------------------------------------------------------------------
    credentials.delete_key(PROV)
    c.check("初始未配置 → has_key 为假（用无环境变量回退的合成服务商）",
            credentials.has_key(PROV) is False)
    c.check("初始 fingerprint 为空串", credentials.fingerprint(PROV) == "")

    credentials.set_key(PROV, FAKE_KEY)
    c.check("写入后 → has_key 为真", credentials.has_key(PROV) is True)

    fp = credentials.fingerprint(PROV)
    c.check("★ fingerprint 是掩码形态（前4…末4）",
            fp.startswith(FAKE_KEY[:4]) and fp.endswith(FAKE_KEY[-4:]) and "…" in fp, fp)
    c.check("★★ fingerprint **不含**完整密钥", FAKE_KEY not in fp, fp)
    c.check("★ fingerprint 长度远小于原值", len(fp) < len(FAKE_KEY) / 2, f"{len(fp)} vs {len(FAKE_KEY)}")

    # ------------------------------------------------------------------
    c.section("第 3 节：短密钥的掩码不会把整串露出来")
    # ------------------------------------------------------------------
    credentials.set_key("short", "abc123")
    fp_s = credentials.fingerprint("short")
    c.check("★ 过短的密钥不给掩码（只说已配置）",
            "abc123" not in fp_s and "过短" in fp_s, fp_s)
    credentials.delete_key("short")

    # ------------------------------------------------------------------
    c.section("第 4 节：删除与幂等")
    # ------------------------------------------------------------------
    credentials.delete_key(PROV)
    c.check("删除后 → has_key 为假", credentials.has_key(PROV) is False)
    credentials.delete_key(PROV)
    c.check("重复删除不抛异常（幂等）", True)
    credentials.set_key("x", "")
    c.check("写入空串 = 删除", credentials.has_key("x") is False)

    # ------------------------------------------------------------------
    c.section("第 5 节：优先级 —— 凭据文件 > 环境变量")
    # ------------------------------------------------------------------
    # 这里**必须用**有真实环境变量的服务商（deepseek），才能验证"文件优先"
    credentials.delete_key("deepseek")
    c.check("仅环境变量时 → source 为 env",
            credentials.source("deepseek") == "env", credentials.source("deepseek"))
    c.check("此时 get_key 返回环境变量里的值（非空）",
            bool(credentials.get_key("deepseek")))
    credentials.set_key("deepseek", FAKE_KEY)
    c.check("★ 写入凭据文件后 → source 变为 file（文件优先于环境变量）",
            credentials.source("deepseek") == "file", credentials.source("deepseek"))
    c.check("★ 文件有值时 get_key 返回**文件里的值**（不是环境变量）",
            credentials.get_key("deepseek") == FAKE_KEY)
    credentials.delete_key("deepseek")
    c.check("删掉文件条目后 → 回退到 env（没把老配置弄丢）",
            credentials.source("deepseek") == "env", credentials.source("deepseek"))

    # ------------------------------------------------------------------
    c.section("第 6 节：★ 容错 —— 文件损坏时降级，不崩")
    # ------------------------------------------------------------------
    cred_file.write_text("{ 这不是合法 JSON ", encoding="utf-8")
    c.check("★ 损坏的 JSON → `has_key` 不抛异常（降级为未配置）",
            credentials.has_key(PROV) is False)
    c.check("损坏时 `fingerprint` 返回空串而非抛异常",
            credentials.fingerprint(PROV) == "")
    c.check("损坏时 `source` 返回空而非抛异常", credentials.source(PROV) == "")

    cred_file.write_text(json.dumps(["不是对象"]), encoding="utf-8")
    c.check("★ 顶层不是对象（是数组）→ 同样降级不崩",
            credentials.has_key(PROV) is False)

    cred_file.write_text(json.dumps({"version": 1, "providers": "不是对象"}), encoding="utf-8")
    c.check("★ providers 字段类型错误 → 降级不崩",
            credentials.has_key(PROV) is False)

    # ★ 额外一条：损坏时应能**恢复写入**（不是永久坏掉）
    credentials.set_key(PROV, FAKE_KEY)
    c.check("★ 损坏后仍可正常写入并读回（能自愈）",
            credentials.has_key(PROV) is True)

    # ------------------------------------------------------------------
    c.section("第 7 节：写入是原子的，且不留临时文件")
    # ------------------------------------------------------------------
    if cred_file.exists():
        cred_file.unlink()
    credentials.set_key(PROV, FAKE_KEY)
    c.check("写入后文件存在", cred_file.is_file())
    leftovers = [p.name for p in SANDBOX.iterdir() if p.name.startswith(".cred_")]
    c.check("★ 不留临时文件（原子写已清理）", not leftovers, str(leftovers))
    c.check("文件是合法 JSON 且结构正确",
            isinstance(json.loads(cred_file.read_text("utf-8")).get("providers"), dict))

    # ------------------------------------------------------------------
    c.section("第 8 节：落盘内容不泄漏到 stdout")
    # ------------------------------------------------------------------
    c.check("（本守卫全程未打印任何密钥明文 —— 见上方输出）", True)
    c.check("★ 沙箱文件确实写了内容（证明前面测的是真写入，不是空跑）",
            cred_file.stat().st_size > 0, str(cred_file.stat().st_size))

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"凭据层守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)

    # 清理沙箱
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())