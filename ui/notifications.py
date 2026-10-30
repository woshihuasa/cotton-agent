# -*- coding: utf-8 -*-
"""通知中心的界面件 —— 入口按钮（带角标）与右侧抽屉。

【为什么单独一个模块】它们是**客户端级**的部件，不属于任何会话。
放在 `ui/notifications.py` 里，主窗口只负责摆放与接线，方便单独渲染预览
（`tests/preview_notifications.py` 就是干这个的，不需要真窗口也能出图）。

【两个部件】
    · `NotificationButton` —— 图标按钮 + 红色角标。角标是**子控件**不是画上去的：
      父控件的 `paintEvent` 先于子控件，直接画会被按钮盖住。
      数字封顶 `9+`，否则三位数会把圆点撑破。
    · `NotificationDrawer` —— 右侧抽屉。只管渲染与发信号，
      开关与动画由主窗口负责（跟左边栏 `_sidebar_anim` 那套一致）。

【三条已经定下的口径】
    · 打开抽屉**即全部已读**；
    · 正文不折叠，直接展开 —— 点开一个要先展开才看得见的列表是最差的交互；
    · 条目之间用一条浅灰细线分隔，不做卡片。卡片会把一屏切成几块，
      而这里的内容本来就是一列流水。
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

#: 抽屉的宽高比（宽 : 高 = 9 : 16）。宽度按可用高度现算，见 `drawer_width_for()`。
DRAWER_RATIO = 9 / 16
#: 现算结果的上下限 —— 太窄放不下中文正文，太宽会把聊天区挤没
DRAWER_MIN_WIDTH = 280
DRAWER_MAX_WIDTH = 420
#: 拿不到高度时用的兜底宽度
DEFAULT_NOTIFY_WIDTH = 315

#: 角标超过这个数就显示 `9+`
BADGE_MAX = 9

NOTIFY_STYLE = """
QWidget#notifyEntry { background: transparent; }
QToolButton#notifyEntryBtn {
    background: transparent;
    border: none;
    border-radius: 6px;
}
QToolButton#notifyEntryBtn:hover { background: rgba(0, 0, 0, 0.06); }
QToolButton#notifyEntryBtn:pressed { background: rgba(0, 0, 0, 0.10); }

/* ★ 角标的 border 与工具栏同色，作用是在红点外面留一圈「白边」——
   手机上那种角标都是这样浮在图标上的，直接贴上去会糊成一团。 */
QLabel#notifyBadge {
    background-color: #E5484D;
    color: #FFFFFF;
    border: 2px solid #F7F7F8;
    border-radius: 9px;
    font-size: 10px;
    font-weight: 600;
}

QFrame#notifyDrawer {
    background-color: #FFFFFF;
    border-left: 1px solid #E3E3E6;
}
QFrame#notifyDrawer QLabel,
QFrame#notifyDrawer QPushButton,
QFrame#notifyDrawer QToolButton {
    background: transparent;
}
/* ★ 容器也要点名清掉：全局 `QWidget` 规则会给 QStackedWidget / 滚动区
   也铺一层底色，空态那张图里整块灰就是这么来的（透明页只是把底露了出来）。
   用 id 选择器压住，是因为它比类型选择器优先。 */
QStackedWidget#notifyStack,
QWidget#notifyEmptyPage,
QScrollArea#notifyScroll,
QWidget#notifyScrollBody {
    background: transparent;
}

QLabel#notifyHeader {
    font-size: 13px;
    font-weight: 600;
    color: #1f1f1f;
}
QPushButton#notifyAllRead {
    background: transparent;
    border: none;
    color: #1A73E8;
    font-size: 11px;
}
QPushButton#notifyAllRead:hover { text-decoration: underline; }
QToolButton#notifyClose {
    background: transparent;
    border: none;
    color: #8A8A8E;
    font-size: 15px;
}
QToolButton#notifyClose:hover { color: #1f1f1f; }

QFrame#notifyItem { background: transparent; border: none; }
QLabel#notifyItemTitle { font-size: 12px; font-weight: 600; color: #1f1f1f; }
QLabel#notifyItemTime  { font-size: 11px; color: #9A9A9E; }
QLabel#notifyItemBody  { font-size: 12px; color: #454549; }
QFrame#notifySep { background-color: #EFEFF1; border: none; }

QPushButton#notifyLink {
    background: transparent;
    border: none;
    color: #1A73E8;
    font-size: 11px;
    padding: 0;
    text-align: left;
}
QPushButton#notifyLink:hover { text-decoration: underline; }
QLabel#notifyEmpty { font-size: 12px; color: #9A9A9E; }
"""


def drawer_width_for(height: int) -> int:
    """按 9:16 现算抽屉宽度，夹在上下限之间。

    ★ 不写死像素：抽屉是纵向铺满的，窗口拉高之后固定宽度会显得又窄又长；
      聊天区的宽度本来也随窗口变，抽屉跟着变才不会一头大一头小。
    """
    try:
        h = int(height)
    except (TypeError, ValueError):
        return DEFAULT_NOTIFY_WIDTH
    if h <= 0:
        return DEFAULT_NOTIFY_WIDTH
    return max(DRAWER_MIN_WIDTH, min(DRAWER_MAX_WIDTH, round(h * DRAWER_RATIO)))


def _make_bubble_icon(size: int = 18, color: str = "#3A3A3C") -> QIcon:
    """画一个对话气泡（描边，不填充）。

    ★ 形状取自 Lucide 的 `message-square`（ISC 许可，24×24 描边路径），
      按 24 单位坐标系**重新落成 QPainterPath**，不引入外部 SVG 文件 ——
      一个图标换一个依赖、一份署名，不值得。这里只近似了它的圆角弧度，
      18px 下看不出来。
    ★ 按 `devicePixelRatio` 放大再缩放回去 —— 高 DPI 下画 18×18 会糊。
    """
    app = QApplication.instance()
    dpr = float(app.devicePixelRatio()) if app is not None else 1.0
    dpr = max(1.0, min(dpr, 3.0))
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # 24 单位坐标系 → 目标像素
    scale = (size * dpr) / 24.0
    p.setTransform(QTransform().scale(scale, scale))

    path = QPainterPath()
    path.moveTo(22.0, 17.0)              # 右边，右下圆角之前
    path.quadTo(22.0, 19.0, 20.0, 19.0)  # 右下角
    path.lineTo(6.8, 19.0)               # 底边
    path.quadTo(5.9, 19.0, 5.3, 19.6)    # 底边转角（气泡的小尾巴）
    path.lineTo(3.2, 21.8)               # 尾巴尖端
    path.lineTo(2.0, 21.3)
    path.lineTo(2.0, 5.0)                # 左边
    path.quadTo(2.0, 3.0, 4.0, 3.0)      # 左上角
    path.lineTo(20.0, 3.0)               # 顶边
    path.quadTo(22.0, 3.0, 22.0, 5.0)    # 右上角
    path.closeSubpath()

    pen = QPen(QColor(color))
    pen.setWidthF(2.0)                   # 24 单位下的描边宽度
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.end()
    return QIcon(pm)


class NotificationButton(QWidget):
    """工具栏右端的入口。角标是子控件，不是画上去的（见模块 docstring）。"""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notifyEntry")
        # ★ 比按钮高 2 像素：角标压在图标上沿之上，若与控件同高会被裁掉
        self.setFixedSize(38, 34)

        self._btn = QToolButton(self)
        self._btn.setObjectName("notifyEntryBtn")
        self._btn.setIcon(_make_bubble_icon(19))
        self._btn.setIconSize(QSize(19, 19))
        self._btn.setFixedSize(32, 32)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self.clicked)
        self._btn.move(2, 2)

        self._badge = QLabel(self)
        self._badge.setObjectName("notifyBadge")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.hide()

        self._unread = 0
        self.set_unread(0)

    def set_unread(self, n: int) -> None:
        """更新角标。0 时隐藏；超过 9 显示 `9+`。"""
        self._unread = max(0, int(n))
        if self._unread <= 0:
            self._badge.hide()
            self._btn.setToolTip("推送（暂无未读）")
            return
        text = str(self._unread) if self._unread <= BADGE_MAX else f"{BADGE_MAX}+"
        self._badge.setText(text)
        # 1 位数是个圆点，2 位数（`9+` 或 `10`）自然拉成胶囊
        width = 18 if len(text) <= 1 else 24
        self._badge.setFixedSize(width, 18)
        self._badge.show()
        self._badge.raise_()
        self._position_badge()
        self._btn.setToolTip(f"推送（{self._unread} 条未读）")

    def unread(self) -> int:
        return self._unread

    def _position_badge(self) -> None:
        # 角标压在图标右上角，并允许超出本体一点点 —— 手机上的角标都是这么放的
        x = self._btn.x() + self._btn.width() - self._badge.width() + 5
        self._badge.move(max(0, x), 0)

    def resizeEvent(self, event) -> None:                      # noqa: N802
        super().resizeEvent(event)
        self._position_badge()


class _NotifyItem(QFrame):
    """一条推送：标题 + 时间 + 正文 + 存档入口。**平铺，不做卡片。**"""

    archive_requested = pyqtSignal(str)

    def __init__(self, item, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notifyItem")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 12, 2, 12)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel(str(item.title or item.job_id or "推送"))
        title.setObjectName("notifyItemTitle")
        head.addWidget(title, 1)

        stamp = QLabel(_fmt_time(item.created_at))
        stamp.setObjectName("notifyItemTime")
        head.addWidget(stamp, 0)
        lay.addLayout(head)

        body = QLabel(str(item.body or ""))
        body.setObjectName("notifyItemBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(body)

        if item.path:
            link = QPushButton("打开存档")
            link.setObjectName("notifyLink")
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.clicked.connect(lambda: self.archive_requested.emit(str(item.path)))
            lay.addWidget(link, 0, Qt.AlignmentFlag.AlignLeft)


def _separator() -> QFrame:
    """条目之间的浅灰细线（一像素，不占间距）。"""
    line = QFrame()
    line.setObjectName("notifySep")
    line.setFixedHeight(1)
    line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return line


def _fmt_time(text: str) -> str:
    """`2026-10-30T08:00:00` → `10-30 08:00`。读不懂就原样显示。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).strftime("%m-%d %H:%M")
    except ValueError:
        return raw


class NotificationDrawer(QFrame):
    """右侧抽屉。只管渲染与发信号，开关动画由主窗口管。"""

    closed = pyqtSignal()
    all_read_requested = pyqtSignal()
    archive_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notifyDrawer")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(4)
        title = QLabel("推送")
        title.setObjectName("notifyHeader")
        head.addWidget(title, 1)

        self._all_read = QPushButton("全部已读")
        self._all_read.setObjectName("notifyAllRead")
        self._all_read.setCursor(Qt.CursorShape.PointingHandCursor)
        self._all_read.clicked.connect(self.all_read_requested)
        head.addWidget(self._all_read, 0)

        close = QToolButton()
        close.setObjectName("notifyClose")
        close.setText("×")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.closed)
        head.addWidget(close, 0)
        lay.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("notifyScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container.setObjectName("notifyScrollBody")
        self._items = QVBoxLayout(self._container)
        self._items.setContentsMargins(0, 0, 0, 0)
        self._items.setSpacing(0)
        self._items.addStretch(1)
        self._scroll.setWidget(self._container)

        self._empty = QLabel("还没有推送。\n每天八点的农情简报会出现在这里。")
        self._empty.setObjectName("notifyEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        empty_page = QWidget()
        empty_page.setObjectName("notifyEmptyPage")
        el = QVBoxLayout(empty_page)
        el.setContentsMargins(0, 0, 0, 0)
        el.addStretch(1)
        el.addWidget(self._empty)
        el.addStretch(1)

        # ★ 空态与列表放在**同一个堆叠容器**里，外层只留一个可伸缩控件。
        #   直接往外层加弹性 spacer 的话，它们会跟滚动区抢高度 ——
        #   出图时看到的第一版就是这样：列表被挤成三分之一，正文被截断。
        self._stack = QStackedWidget()
        self._stack.setObjectName("notifyStack")
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(empty_page)
        lay.addWidget(self._stack, 1)

        self._count = 0

    # ---- 渲染 ----

    def set_items(self, items: Sequence) -> None:
        """整表重建。条目数量是个位数，重建比增量维护简单，也不会漏删。"""
        while self._items.count() > 1:            # 保留末尾的 stretch
            w = self._items.takeAt(0)
            wid = w.widget()
            if wid is not None:
                wid.deleteLater()

        rows = list(items)
        for i, item in enumerate(rows):
            row = _NotifyItem(item, self._container)
            row.archive_requested.connect(self.archive_requested)
            self._items.insertWidget(self._items.count() - 1, row)
            if i < len(rows) - 1:
                self._items.insertWidget(self._items.count() - 1, _separator())

        self._count = len(rows)
        self._stack.setCurrentIndex(1 if self._count == 0 else 0)
        self._all_read.setVisible(self._count > 0)
        self._scroll.verticalScrollBar().setValue(0)

    def item_count(self) -> int:
        return self._count