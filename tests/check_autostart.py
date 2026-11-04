# -*- coding: utf-8 -*-
"""开机自启守卫 —— 默认不写注册表，只有用户勾选才写。

【为什么这么小心】写注册表是"程序在系统里留痕"。这里最怕两种错：

  ① **默认就写了**：用户装完什么都没做，重启电脑却发现多了个自启项 —— 那是失礼，
     而且很难删（他还得知道去注册表里找）。
  ② **判定看"值存在"而不是"值等于自己"**：安装目录挪过之后，Run 里残留的是旧路径，
     开机时会去启动一个不存在的 exe。所以判定必须比完整命令。

【零真实注册表】后端是可注入的，守卫全程用内存实现，绝不碰用户注册表。
【用法】python tests/check_autostart.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_autostart_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from core import autostart as A                                 # noqa: E402


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


def section_default_off(c: Checker) -> None:
    c.section("第 1 节：★ 默认不写注册表")

    mem = A._MemBackend("")
    A.use_backend(mem)
    try:
        c.check("注册表为空时 `enabled()` 为假", A.enabled() is False)
        c.check("★ 只是查询不产生任何写入",
                mem.writes == [] and mem.deletes == 0,
                f"{mem.writes} / {mem.deletes}")
    finally:
        A.use_backend(None)


def section_enable(c: Checker) -> None:
    c.section("第 2 节：勾选才写，取消就删")

    exe = r"C:\Program Files\Cotton Agent\CottonAgent.exe"
    mem = A._MemBackend("")
    A.use_backend(mem)
    real_frozen = getattr(sys, "frozen", False)
    real_exe = sys.executable
    try:
        # 假装是打包版：命令里用的是 sys.executable
        sys.frozen = True                                # type: ignore[attr-defined]
        sys.executable = exe
        cmd = A.command_for_this_app()
        c.check("★ 启动命令带引号（路径有空格也不会被截断）",
                cmd == f'"{exe}"', cmd)
        c.check("可用性检查通过（Windows + 打包版）",
                A.unavailable_reason() == "", A.unavailable_reason())

        ok, msg = A.set_enabled(True)
        c.check("★ 勾选后写入注册表", ok and mem.value == f'"{exe}"', f"{ok} {msg}")
        c.check("写入后 `enabled()` 为真", A.enabled() is True)

        # 幂等：再勾一次仍是同一个值，不会写两遍不同的东西
        before = len(mem.writes)
        A.set_enabled(True)
        c.check("重复勾选不会写出不一样的值",
                set(mem.writes) == {f'"{exe}"'} and len(mem.writes) == before + 1,
                str(mem.writes))

        ok, _ = A.set_enabled(False)
        c.check("★ 取消后删掉注册表项", ok and mem.value == "", mem.value)
        c.check("删掉后 `enabled()` 为假", A.enabled() is False)
        ok, _ = A.set_enabled(False)
        c.check("重复取消不报错（幂等）", ok)
    finally:
        if real_frozen:
            sys.frozen = real_frozen                  # type: ignore[attr-defined]
        else:
            try:
                del sys.frozen                            # type: ignore[attr-defined]
            except AttributeError:
                pass
        sys.executable = real_exe
        A.use_backend(None)


def section_path_moved(c: Checker) -> None:
    c.section("第 3 节：★ 安装目录挪走后不能认成「已启用」")

    old_exe = r"D:\old\CottonAgent.exe"
    mem = A._MemBackend(f'"{old_exe}"')
    A.use_backend(mem)
    real_frozen = getattr(sys, "frozen", False)
    real_exe = sys.executable
    try:
        sys.frozen = True                                # type: ignore[attr-defined]
        sys.executable = r"E:\new\CottonAgent.exe"
        c.check("★ 注册表里是旧路径 → 判为未启用", A.enabled() is False,
                mem.value)
        A.set_enabled(True)
        c.check("勾选后覆盖成新路径",
                mem.value == '"E:\\new\\CottonAgent.exe"', mem.value)
        c.check("覆盖后判为已启用", A.enabled() is True)
    finally:
        if real_frozen:
            sys.frozen = real_frozen                  # type: ignore[attr-defined]
        else:
            try:
                del sys.frozen                            # type: ignore[attr-defined]
            except AttributeError:
                pass
        sys.executable = real_exe
        A.use_backend(None)


def section_dev_mode(c: Checker) -> None:
    c.section("第 4 节：★ 开发模式下禁用，且不写注册表")

    mem = A._MemBackend("")
    A.use_backend(mem)
    try:
        # 守卫本身就跑在开发模式下
        c.check("开发模式：`packaged()` 为假", A.packaged() is False)
        why = A.unavailable_reason()
        c.check("★ 给出可读原因", "开发模式" in why, why)
        c.check("开发模式下取不到启动命令", A.command_for_this_app() == "")
        ok, msg = A.set_enabled(True)
        c.check("★ 开发模式下拒绝写入，且注册表没被动过",
                ok is False and mem.value == "" and mem.writes == [],
                f"{ok} {msg} / {mem.writes}")
        c.check("开发模式下 `enabled()` 恒为假（不会误显示成已开）",
                A.enabled() is False)
    finally:
        A.use_backend(None)


def section_ui(c: Checker) -> None:
    c.section("第 5 节：设置界面的开关")

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from ui.settings_dialog import SettingsDialog

    mem = A._MemBackend("")
    A.use_backend(mem)
    try:
        dlg = SettingsDialog()
        c.check("设置里有「开机时自动启动」复选框", hasattr(dlg, "_autostart"))
        c.check("文案正确", "开机" in dlg._autostart.text(), dlg._autostart.text())
        c.check("★ 默认不勾选", dlg._autostart.isChecked() is False,
                str(dlg._autostart.isChecked()))
        c.check("★ 打开设置不写注册表",
                mem.writes == [] and mem.deletes == 0, str(mem.writes))
        c.check("开发模式下复选框是禁用的（写进去也没意义）",
                dlg._autostart.isEnabled() is False)
        dlg.close()
    finally:
        A.use_backend(None)

    src = (ROOT / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    c.check("界面不直接碰注册表（只经 `core.autostart`）",
            "winreg" not in src, "")
    c.check("保存时才写，且仅在状态变化时写",
            "want != _autostart.enabled()" in src, "")


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_default_off(c)
        section_enable(c)
        section_path_moved(c)
        section_dev_mode(c)
        section_ui(c)
    finally:
        print()
        print("=" * 78)
        print(f"开机自启守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        import shutil
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())