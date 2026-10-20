# -*- coding: utf-8 -*-
"""UI 属性守卫：`self.x` 有读无定义（AttributeError 的静态拦截）。

【它守的那一类 bug】

    界面里重命名一个属性（比如把"思维链长度"从**下拉框**改成**按钮**，
    `_effort_combo` → `_effort_level`），只要漏改一处，
    运行到那行就抛 `AttributeError` —— 而且是**在用户面前**抛。

    实测就发生过一次：`_on_submit` 里仍然读 `self._effort_combo.currentData()`，
    结果**任何一条消息都发不出去**，用户只看到「程序错误」弹窗。

【为什么静态能查】

    `self.x` 的读取如果能解析到同类的：
      · 方法定义（`def x`）
      · 类级常量（`x = ...` / `x: T = ...`）
      · 方法体里的赋值（`self.x = ...`）
      · Qt 基类继承来的（`close()` / `resize()` …）
    就不是地雷。**一个都解析不到，就是必然的 AttributeError。**

【局限（必须说清）】
    · 只覆盖 `ui/*.py`（界面是唯一用大量 `self._x` 的地方）
    · Qt 继承方法用**白名单**近似，不是真正的类型推导 ——
      新用到没列进去的 Qt 方法会**假报**。白名单在下面，按需补。
    · 不查 `setattr(self, ...)` 动态属性（那本来就没法静态查）

【用法】python tests/check_ui_attrs.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


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


#: Qt 基类 / 常见控件的方法名（继承而来，源码里当然不会赋值）。
#:
#: ★ 这是**白名单近似**：新用到没列进来的 Qt 方法会假报，
#:   届时把它加到这里即可（比漏掉真 bug 划算）。
QT_NAMES: frozenset[str] = frozenset("""
accept reject close show hide resize move setFixedSize setMinimumSize
setMaximumWidth setMinimumWidth setStyleSheet setObjectName setWindowTitle
setCursor setToolTip setAttribute setPixmap setModal setEchoMode echoMode
setCentralWidget centralWidget width height rect update updateGeometry
parent parentWidget mapFromGlobal raise_ frameGeometry addAction action
isVisible isEnabled setEnabled setLayout layout adjustSize sizeHint
window testAttribute setText text currentData setCurrentIndex currentIndex
setFocusPolicy setWordWrap setTextFormat setTextInteractionFlags
setSizePolicy setAlignment setMinimumHeight setMinimumWidth setContentsMargins
blockSignals setSelectionMode setIconSize setIcon setStyleSheet
""".split())


def scan_file(path: Path) -> list[tuple[str, int]]:
    """返回 `[(类名.属性, 行号), ...]` —— 有读无定义的那些。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[tuple[str, int]] = []

    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        defined: set[str] = set()

        # ① 类体：方法名 与 类级常量
        for stmt in cls.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                tgts = (stmt.targets if isinstance(stmt, ast.Assign)
                        else [stmt.target])
                for tg in tgts:
                    if isinstance(tg, ast.Name):
                        defined.add(tg.id)
            elif isinstance(stmt, ast.FunctionDef):
                defined.add(stmt.name)

        # ② 方法体内 `self.x = ...` / `self.x: T = ...`
        for node in ast.walk(cls):
            if isinstance(node, ast.Assign):
                for tg in node.targets:
                    if (isinstance(tg, ast.Attribute)
                            and isinstance(tg.value, ast.Name)
                            and tg.value.id == "self"):
                        defined.add(tg.attr)
            elif isinstance(node, ast.AnnAssign):
                tg = node.target
                if (isinstance(tg, ast.Attribute)
                        and isinstance(tg.value, ast.Name)
                        and tg.value.id == "self"):
                    defined.add(tg.attr)

        # ③ 读
        for node in ast.walk(cls):
            if not isinstance(node, ast.Attribute):
                continue
            if not (isinstance(node.value, ast.Name)
                    and node.value.id == "self"):
                continue
            nm = node.attr
            if nm in defined or nm in QT_NAMES or nm.startswith("__"):
                continue
            problems.append((f"{cls.name}.{nm}", getattr(node, "lineno", 0)))

    # 去重（同名只报第一次）
    seen: set[str] = set()
    out: list[tuple[str, int]] = []
    for k, ln in problems:
        if k in seen:
            continue
        seen.add(k)
        out.append((k, ln))
    return out


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("① 扫描 ui/*.py：`self.x` 有读无定义")
    # ------------------------------------------------------------------
    ui_dir = ROOT / "ui"
    files = sorted(ui_dir.glob("*.py"))
    c.check("★ ui/ 下有文件可扫", bool(files), str(ui_dir))

    all_problems: dict[str, list[tuple[str, int]]] = {}
    for f in files:
        got = scan_file(f)
        c.check(f"{f.name:<22} 无「有读无定义」属性",
                not got,
                "; ".join(f"{n}@行{ln}" for n, ln in got) or "")
        if got:
            all_problems[f.name] = got

    # ------------------------------------------------------------------
    c.section("② 反向验证：扫描器**真的能抓到**问题（防止它形同虚设）")
    # ------------------------------------------------------------------
    # ★ 这一条是本文件最重要的断言：一个永远返回"没问题"的扫描器
    #   比没有扫描器更糟 —— 它会给人虚假的安全感。
    #   所以造一段**已知有问题**的源码，断言扫描器报出它。
    import tempfile

    BAD = '''
class _Fake(QWidget):
    def __init__(self):
        super().__init__()
        self._ok_attr = 1

    def run(self):
        self._not_defined_anywhere   # ← 应该被抓到
        return self._ok_attr
'''
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "_fake.py"
        bad.write_text(BAD, encoding="utf-8")
        got = scan_file(bad)
    names = [n for n, _ in got]
    c.check("★★★ 扫描器能抓到已知的未定义属性（不是空转）",
            any("_not_defined_anywhere" in n for n in names), str(names))
    c.check("★ 且不误报已定义的那个",
            not any("_ok_attr" in n for n in names), str(names))

    print()
    print("=" * 78)
    print(f"UI 属性守卫: {c.passed}/{c.total} 通过")
    if all_problems:
        print()
        print("★ 修法：给每个报出来的属性在 `__init__` 里赋值，")
        print("  或（若该属性已改名）把读取处改成新名字。")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())