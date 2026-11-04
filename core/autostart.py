# -*- coding: utf-8 -*-
"""开机自启 —— 写一个 HKCU 的 Run 项，默认不写。

【为什么默认关】写注册表是"程序在系统里留痕"，这种事必须由用户主动点头。
  默认关意味着：装完之后重启电脑，程序不会自己起来；只有用户在设置里勾上，
  才会真的注册。反过来（默认开）会让人在不知情的情况下多一个自启项。

【为什么只对打包版生效】开发模式跑的是 `python main.py`，注册进去的会是
  `pythonw.exe main.py` 这种命令行，换个目录就失效，还会在注册表里留垃圾。
  所以未打包时这个开关是禁用的，旁边写清楚原因。

【为什么要比对完整路径】用户可能把安装目录挪走。老路径留在 Run 里，开机时
  会尝试启动一个不存在的程序 —— 系统报错、用户困惑。所以判定"已启用"的
  条件是：注册表里的命令**正好等于本程序当前的路径**，不一致就当没启用，
  下次勾选时覆盖成新路径。

【可测试】注册表访问被一层 `_Backend` 隔开，守卫用内存实现替换真实注册表，
  绝不碰用户的注册表。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Protocol

log = logging.getLogger("AutoStart")

#: 注册表位置与值名。只写当前用户，不需要管理员权限。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CottonAgent"


class _Backend(Protocol):
    def get(self) -> str: ...
    def put(self, command: str) -> None: ...
    def delete(self) -> None: ...


class _WinRegBackend:
    """真实注册表。只在 Windows 上可用。"""

    def get(self) -> str:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                value, _ = winreg.QueryValueEx(k, VALUE_NAME)
                return str(value or "")
        except FileNotFoundError:
            return ""
        except OSError as e:
            log.warning("读注册表失败（按未启用处理）：%s", e)
            return ""

    def put(self, command: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, VALUE_NAME)
        except FileNotFoundError:
            return


class _MemBackend:
    """内存实现（守卫用）。"""

    def __init__(self, initial: str = "") -> None:
        self.value = initial
        self.writes: list[str] = []
        self.deletes = 0

    def get(self) -> str:
        return self.value

    def put(self, command: str) -> None:
        self.value = command
        self.writes.append(command)

    def delete(self) -> None:
        self.value = ""
        self.deletes += 1


_backend: _Backend | None = None


def _get_backend() -> _Backend:
    global _backend
    if _backend is None:
        _backend = _WinRegBackend()
    return _backend


def use_backend(backend: _Backend | None) -> None:
    """替换注册表后端。传 `None` 恢复真实注册表（守卫用）。"""
    global _backend
    _backend = backend


def supported() -> bool:
    """当前平台能不能写自启项。"""
    return sys.platform.startswith("win")


def packaged() -> bool:
    """是不是打包后的 exe。开发模式下没法注册有意义的启动命令。"""
    return bool(getattr(sys, "frozen", False))


def unavailable_reason() -> str:
    """为什么这个开关现在不能用。空串表示可用。"""
    if not supported():
        return "当前系统不支持（只实现了 Windows）"
    if not packaged():
        return "开发模式不支持，装成 exe 后才能用"
    return ""


def command_for_this_app() -> str:
    """本程序的自启命令行。带引号，防止路径里有空格时被截断。"""
    if not packaged():
        return ""
    exe = str(sys.executable)
    return f'"{exe}"'


def enabled() -> bool:
    """当前**本程序**是否已注册自启。

    ★ 只认完全相等：注册表里若是一个旧路径，说明安装目录挪过，
      按"未启用"处理，避免开机时去启动一个不存在的程序。
    """
    if not supported():
        return False
    cmd = command_for_this_app()
    if not cmd:
        return False
    return _get_backend().get().strip() == cmd


def set_enabled(on: bool) -> tuple[bool, str]:
    """启用或关闭自启。返回 `(是否成功, 说明)`。"""
    reason = unavailable_reason()
    if reason:
        return False, reason
    cmd = command_for_this_app()
    if not cmd:
        return False, "取不到本程序的路径"
    try:
        if on:
            _get_backend().put(cmd)
            log.info("已注册开机自启：%s", cmd)
            return True, "已设置为开机自启"
        _get_backend().delete()
        log.info("已取消开机自启")
        return True, "已取消开机自启"
    except OSError as e:
        log.warning("写自启项失败：%s", e)
        return False, f"写注册表失败：{e}"


def remove_if_present() -> None:
    """卸载或迁移时清掉残留项（幂等）。"""
    if not supported():
        return
    try:
        if _get_backend().get():
            _get_backend().delete()
    except OSError as e:
        log.warning("清理自启项失败：%s", e)


__all__ = ["RUN_KEY", "VALUE_NAME", "command_for_this_app", "enabled",
           "packaged", "remove_if_present", "set_enabled", "supported",
           "unavailable_reason", "use_backend"]