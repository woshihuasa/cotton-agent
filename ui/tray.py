# -*- coding: utf-8 -*-
"""系统托盘 —— 关窗不退出时，程序的门面就是这个图标。

【为什么需要它】
  定时推送要做到"用户没开窗口也能到"，进程就得常驻。而常驻的界面不能只剩一个
  看不见的进程：用户需要知道它还在跑、需要能把它叫回来、也需要能真正关掉它。
  托盘菜单就是这三件事的入口。

【图标怎么来的】
  仓库里没有 `.ico`，所以按通知图标那套用 `QPainter` 现画：深色圆角方块上三团
  白色棉铃。**每个尺寸单独画一份**，而不是画一张再让 Qt 缩放 —— 在已设
  `devicePixelRatio` 的 pixmap 上再画会二次缩放，16 像素那张会被裁掉一角
  （通知图标踩过这个坑）。

【什么时候不显示托盘】
  系统没有托盘（某些精简版 Windows、或远程会话里）时，`QSystemTrayIcon.isSystemTrayAvailable()`
  为假。这时**不能**把关窗改成隐藏 —— 否则用户关不掉窗口，也找不到它。
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

log = logging.getLogger("Tray")

#: 要画的图标尺寸。16 是经典通知区，24 是高分屏，32 与 48 给任务栏预览与提示
ICON_SIZES: tuple[int, ...] = (16, 24, 32, 48)

_BG = QColor("#1F2328")
_FG = QColor("#FFFFFF")
_STEM = QColor("#8A8A8E")


def _draw_one(size: int) -> QPixmap:
    """画一张 `size` 见方的图标。**按该尺寸原生绘制，不做缩放。**"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 圆角底：留 1 像素边距，避免贴边被系统裁掉
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_BG)
    radius = size * 0.24
    p.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), radius, radius)

    # 三团棉铃：上中、左下、右下。半径随尺寸缩放，最小也有 1.6 像素。
    r = max(1.6, size * 0.16)
    cx, cy = size / 2.0, size / 2.0
    puffs = ((cx, cy - size * 0.14), (cx - size * 0.16, cy + size * 0.12),
             (cx + size * 0.16, cy + size * 0.12))
    p.setBrush(_FG)
    for x, y in puffs:
        p.drawEllipse(QRectF(x - r, y - r, r * 2, r * 2))

    # 底部茎：一条短竖线，让它在 16 像素时也能看出是"果实"而不是三个点
    p.setPen(_STEM)
    p.setBrush(_STEM)
    p.drawRect(QRectF(cx - max(0.5, size * 0.02), cy + size * 0.28,
                      max(1.0, size * 0.04), max(1.0, size * 0.12)))
    p.end()
    return pm


def app_icon_path():
    """打包内 / 源码里的应用图标路径（取不到返回 None）。"""
    from config import resource_path
    for name in ("ui/icons/app.ico", "ui/icons/app.png"):
        p = resource_path(name)
        if p.exists():
            return p
    return None


def tray_icon() -> QIcon:
    """多尺寸图标。

    优先用 `tools/make_app_icon.py` 生成的那套（大尺寸线稿、小尺寸简化图形），
    它是设计稿的正身；生成物缺失时才退回**现画**的三团棉铃 —— 保证任何情况下
    托盘都有图标，而不是一片空白。
    """
    path = app_icon_path()
    if path is not None:
        icon = QIcon(str(path))
        if not icon.isNull() and icon.availableSizes():
            return icon
    icon = QIcon()
    for s in ICON_SIZES:
        icon.addPixmap(_draw_one(s))
    return icon


def tray_available() -> bool:
    """系统有没有托盘。没有就不该把"关窗"改成"隐藏" —— 否则关不掉。"""
    try:
        return bool(QSystemTrayIcon.isSystemTrayAvailable())
    except Exception as e:                                    # noqa: BLE001
        log.warning("检测托盘失败（按不可用处理）：%s", e)
        return False


class AppTray(QSystemTrayIcon):
    """托盘图标与右键菜单。

    只发信号，不直接改窗口 —— 界面的事留给 `CottonAgentWindow`，
    这样托盘本身可以单独构造与测试。
    """

    show_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(tray_icon(), parent)
        self.setToolTip("棉花种植决策智能体")
        self._menu = QMenu()
        act_show = self._menu.addAction("显示主窗口")
        act_show.triggered.connect(self.show_requested)
        act_settings = self._menu.addAction("推送设置")
        act_settings.triggered.connect(self.settings_requested)
        self._menu.addSeparator()
        act_quit = self._menu.addAction("退出")
        act_quit.triggered.connect(self.quit_requested)
        self.setContextMenu(self._menu)
        # 左键单击也唤起窗口：只给右键的话，很多人会以为点不动
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_requested.emit()

    def notify(self, title: str, body: str, *, ms: int = 6000) -> bool:
        """弹一条系统通知。窗口藏起来时，这是用户唯一能看到推送的入口。

        ★ 用 `showMessage` 而不是 Windows 原生 toast：后者要注册 AppUserModelID
          与快捷方式，打包版才有意义，且不同系统版本行为不一。气泡够用，且
          点击会触发 `messageClicked`。
        """
        if not self.isVisible():
            return False
        try:
            self.showMessage(str(title or "推送"), str(body or ""),
                             QSystemTrayIcon.MessageIcon.Information, int(ms))
            return True
        except Exception as e:                                # noqa: BLE001
            log.warning("托盘气泡弹出失败（已忽略）：%s", e)
            return False

    def menu_action_texts(self) -> list[str]:
        """菜单项文字（守卫用，避免为断言去翻 QMenu 的内部结构）。"""
        return [a.text() for a in self._menu.actions() if not a.isSeparator()]


__all__ = ["AppTray", "ICON_SIZES", "tray_available", "tray_icon"]