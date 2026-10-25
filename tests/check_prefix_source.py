# -*- coding: utf-8 -*-
"""主路径前缀来源守卫 —— **模板不许分叉**。

【为什么需要这一节】逐次调用的缓存实测发现：主路径（工具循环 / 检索 / 最终回答）
的**所有调用共享同一段稳定前缀** —— system 前 600 字与前 1500 字的指纹完全一致，
而 `sys_len` 从 2394 到 3672 的差异**全部落在 1500 字之后的易变段**；
同前缀下排除首次，命中率 80.6%~87.8%（与 `docs/AGENT_RUNTIME.md` 的 83% 吻合）。

这个命中率的**结构前提**是「主路径只有一处组装 system 消息，且都源自 `SYSTEM_PROMPT`」。
若有人给某条路径单独再写一个 system prompt，前缀就会分叉、缓存腰斩，
而 **`check_prompt_layers.py` 现有的断言全都发现不了**（它们只检查 `SYSTEM_PROMPT` 自身）。

本节把那个前提锁住：**模板不许分叉**。

【零 LLM / 零 API / 零网络】全部是 AST 静态检查与纯函数断言。

【用法】python tests/check_prefix_source.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import prompt_layers                              # noqa: E402
from core.rag_engine import SYSTEM_PROMPT                    # noqa: E402

#: 允许出现的 system 角色构造点 —— content 表达式的名字集合。
#: ★ 往里加东西之前先想清楚：它会不会成为**第三段缓存前缀**。
ALLOWED_SYSTEM_SOURCES = frozenset({
    "system_content",            # 主路径：唯一真正发送的 system 消息
    "REWRITE_SYSTEM_PROMPT",     # 查询改写：自带独立前缀（实测确认为另一段）
    "sm",                        # 只是 `_calculate_tokens` 的入参，从不发送
})


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}"
              + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def _system_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """找出所有构造 `{"role": "system", ...}` 的位置，返回 (行号, content 表达式名)。"""
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        roles = {k.value: v for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant)}
        rv = roles.get("role")
        if isinstance(rv, ast.Constant) and rv.value == "system":
            cv = roles.get("content")
            name = (getattr(cv, "id", None) or getattr(cv, "attr", None)
                    or type(cv).__name__)
            sites.append((node.lineno, str(name)))
    return sites


def main() -> int:
    c = Checker()
    src = (ROOT / "core" / "rag_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ------------------------------------------------------------------
    c.section("第 1 节：system 角色构造点（集合必须封闭）")
    # ------------------------------------------------------------------
    sites = _system_sites(tree)
    names = {n for _ln, n in sites}
    n_allowed = len(ALLOWED_SYSTEM_SOURCES)

    c.check(f"★ system 角色构造点恰好 **{n_allowed} 处**"
            "（主路径 / 查询改写 / L3 token 估算）",
            len(sites) == n_allowed, f"实际 {len(sites)} 处: {sites}")
    c.check("★ 内容来源**恰好**是白名单里的那几个（多一个就报）",
            names == set(ALLOWED_SYSTEM_SOURCES), str(sorted(names)))
    c.check("★ 其中必须有 `system_content`（主路径的唯一来源）",
            "system_content" in names, str(names))
    c.check("★ 其中必须有 `REWRITE_SYSTEM_PROMPT`（查询改写，独立前缀）",
            "REWRITE_SYSTEM_PROMPT" in names, str(names))

    # ------------------------------------------------------------------
    c.section("第 2 节：主路径 system 内容只有一个来源")
    # ------------------------------------------------------------------
    assigns = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
               for t in n.targets
               if isinstance(t, ast.Name) and t.id == "system_content"]
    c.check("★ `system_content` 只被赋值**一次**（多处赋值 = 前缀可能分叉）",
            len(assigns) == 1, f"实际 {len(assigns)} 处: 行 {assigns}")

    fmt_n = src.count("SYSTEM_PROMPT.format(")
    c.check("★ 主 system 由 `SYSTEM_PROMPT.format(...)` 组装（恰好一处）",
            fmt_n == 1, f"实际 {fmt_n} 处")

    def_n = src.count("\nSYSTEM_PROMPT = ")
    c.check("★ `SYSTEM_PROMPT` 只定义一次", def_n == 1, f"实际 {def_n} 处")

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 其余模板都不含分层边界（不参与主路径前缀）")
    # ------------------------------------------------------------------
    from core import rag_engine as re_mod

    others = {
        "REWRITE_SYSTEM_PROMPT": re_mod.REWRITE_SYSTEM_PROMPT,
        "SUMMARY_PROMPT": re_mod.SUMMARY_PROMPT,
        "L4_EXTRACT_PROMPT": re_mod.L4_EXTRACT_PROMPT,
        "L4_CONFLICT_PROMPT": re_mod.L4_CONFLICT_PROMPT,
        "L3_COMPRESS_PROMPT": re_mod.L3_COMPRESS_PROMPT,
        "TITLE_PROMPT": re_mod.TITLE_PROMPT,
    }
    c.check("★ `SYSTEM_PROMPT` 含分层边界（上面几节的检查前提）",
            prompt_layers.has_boundary(SYSTEM_PROMPT))

    leaked = [n for n, s in others.items() if prompt_layers.has_boundary(s)]
    c.check(f"★ 其余 {len(others)} 个模板**都不含**分层边界（各自独立，不参与主路径前缀）",
            not leaked, f"含边界的: {leaked}")

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 反向验证（断言不是恒真）")
    # ------------------------------------------------------------------
    fake = dict(others)
    fake["TITLE_PROMPT"] = "x" + prompt_layers.STABLE_BOUNDARY + "y"
    leaked2 = [n for n, s in fake.items() if prompt_layers.has_boundary(s)]
    c.check("★ 给某个模板塞入边界 → 第 3 节的检查**必须报警**",
            leaked2 == ["TITLE_PROMPT"], str(leaked2))

    fake_src = src + '\n_x = [{"role": "system", "content": "另一个 prompt"}]\n'
    fake_sites = _system_sites(ast.parse(fake_src))
    c.check("★ 反向验证：多一处 system 构造点 → 第 1 节必须报警",
            len(fake_sites) == n_allowed + 1, f"实际 {len(fake_sites)} 处")

    fake2 = src + '\nsystem_content = "另拼的一份"\n'
    fake_assigns = [n.lineno for n in ast.walk(ast.parse(fake2))
                    if isinstance(n, ast.Assign) for t in n.targets
                    if isinstance(t, ast.Name) and t.id == "system_content"]
    c.check("★ 反向验证：`system_content` 被赋值两次 → 第 2 节必须报警",
            len(fake_assigns) == 2, f"实际 {len(fake_assigns)} 处")

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"主路径前缀来源守卫: {c.passed}/{c.total} 通过")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())