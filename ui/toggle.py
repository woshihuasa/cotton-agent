# -*- coding: utf-8 -*-
"""开关控件 —— 截图里那种"胶囊 + 圆钮"的开关。

【为什么不用 `QCheckBox`】勾选框表达"启用/停用"功能时读起来像表单里的多选项；
        胶囊开关一眼就知道是"这东西现在开着还是关着"。设置页里功能开关多了之后，
        这个区别很明显。

【实现】`QAbstractButton` 子类，自绘：
        · 轨道：圆角矩形，关=浅灰 `#E3E5E8`，开=深灰 `#1F2328`；
        · 圆钮：白色实心圆，关时在左、开时在右。

【★ 刻意不做位移动画】第一版用 `QPropertyAnimation` 做 110ms 缓动，结果在
        "控件还没显示""事件循环没转"这类情况下动画停在半路，画出来是**开着的
        开关显示成关**（或反过来）。一个状态控件"看到的不等于实际的"比没有动画
        糟得多 —— 所以这里让绘制**永远**由 `isChecked()` 决定，代价是没有缓动。
"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QAbstractButton, QSizePolicy


class ToggleSwitch(QAbstractButton):
    """胶囊开关。`isChecked()` / `setChecked()` 与普通按钮一致，空格键可切换。"""

    TRACK_OFF = QColor("#E3E5E8")
    TRACK_ON = QColor("#1F2328")
    KNOB = QColor("#FFFFFF")

    WIDTH = 40
    HEIGHT = 22
    PAD = 2                      # 圆钮与轨道的边距

    def __init__(self, checked: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        # 状态一变就重绘（没有动画，`paintEvent` 直接读 isChecked()）
        self.toggled.connect(lambda _on: self.update())

    def sizeHint(self) -> QSize:                              # noqa: N802
        return QSize(self.WIDTH, self.HEIGHT)

    def paintEvent(self, _event) -> None:                     # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        on = self.isChecked()
        radius = self.HEIGHT / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.TRACK_ON if on else self.TRACK_OFF)
        p.drawRoundedRect(QRectF(0, 0, self.WIDTH, self.HEIGHT), radius, radius)

        d = self.HEIGHT - self.PAD * 2
        x = (self.WIDTH - d - self.PAD) if on else self.PAD
        p.setBrush(self.KNOB)
        p.drawEllipse(QRectF(x, self.PAD, d, d))
        p.end()


__all__ = ["ToggleSwitch"]