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

from core.notifications import DEFAULT_KEEP_DAYS

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
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

/* 保留期说明：浅灰、居中，只在有消息时出现 */
QLabel#notifyFootNote { font-size: 11px; color: #C2C2C7; }
QLabel#notifyEmpty { font-size: 12px; color: #9A9A9E; }

/* ── 启动浮窗 ──
   ★ 它是主窗口的**子控件**，不是独立窗口：`Qt.Popup` 在 Windows 上会抢焦点，
     用户正在输入框里打字时突然失焦很烦。 */
QFrame#notifyToast {
    background-color: #FFFFFF;
    border: 1px solid #E3E3E6;
    border-radius: 10px;
}
QLabel#toastTitle { font-size: 12px; font-weight: 600; color: #1f1f1f; }
QLabel#toastSub   { font-size: 11px; color: #8A8A8E; }
QPushButton#toastView {
    background: transparent; border: none;
    color: #1A73E8; font-size: 12px; padding: 0;
}
QPushButton#toastView:hover { text-decoration: underline; }
QPushButton#toastDismiss {
    background: transparent; border: none;
    color: #8A8A8E; font-size: 12px; padding: 0;
}
QPushButton#toastDismiss:hover { color: #1f1f1f; }
"""

#: 浮窗宽度
TOAST_WIDTH = 300

#: 浮窗自动收起时间（毫秒）
TOAST_MS = 8000


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
      一个图标换一个依赖、一份署名，不值得。原版的尾巴在 18px 下只剩一两个
      像素，这里把气泡本体压矮一点、把左下角拉出更长的尖角，小尺寸才认得出。
    ★ 高 DPI：**先按设备像素画，画完再给 QPixmap 打 DPR 标记**。
      反过来（先 setDevicePixelRatio 再画）QPainter 会自动再乘一次 DPR，
      路径的右半边和下边跑到画布外 —— 19px 的图标就只剩一个方括号 `⊏`。
    ★ 一个 QIcon 里放 1.0/1.25/1.5/2.0/3.0（外加当前屏幕 DPR）多份原生像素图：
      哪种缩放的屏幕来取都有一份清晰的图，由 QIcon 按目标 DPR 自己挑，不靠事后拉伸。
    """
    size = max(1, int(size))
    app = QApplication.instance()
    screen_dpr = float(app.devicePixelRatio()) if app is not None else 1.0
    screen_dpr = max(1.0, min(screen_dpr, 3.0))

    icon = QIcon()
    for dpr in sorted({1.0, 1.25, 1.5, 2.0, 3.0, screen_dpr}):
        px = max(1, int(round(size * dpr)))
        pm = QPixmap(px, px)             # 画的时候 DPR 还是 1，避免二次缩放
        pm.fill(Qt.GlobalColor.transparent)

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 24 单位坐标系 → 实际设备像素
        p.setTransform(QTransform().scale(px / 24.0, px / 24.0))

        path = QPainterPath()
        path.moveTo(5.5, 3.0)                 # 左上圆角之后
        path.lineTo(18.5, 3.0)                # 顶边
        path.quadTo(21.5, 3.0, 21.5, 6.0)     # 右上圆角
        path.lineTo(21.5, 13.5)               # 右边
        path.quadTo(21.5, 16.5, 18.5, 16.5)   # 右下圆角
        path.lineTo(8.0, 16.5)                # 底边，走到尾巴右根
        path.lineTo(2.5, 21.5)                # 尾巴：左下角向下的尖
        path.lineTo(2.5, 6.0)                 # 左边：从尾巴尖直接上去
        path.quadTo(2.5, 3.0, 5.5, 3.0)       # 左上圆角
        path.closeSubpath()

        pen = QPen(QColor(color))
        pen.setWidthF(2.0)                    # 24 单位下的描边宽度
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

        # 画完再标记：逻辑尺寸仍是 size，设备像素是 px
        pm.setDevicePixelRatio(px / float(size))
        icon.addPixmap(pm)
    return icon


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
    """一条推送：标题 + 时间 + 正文。**平铺，不做卡片，不再提供存档入口。**"""

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

        # ★ 「打开存档」已按用户要求去掉：抽屉里直接读正文就够，
        #   再给一个跳文件的入口只是多一次点击。存档本身仍然照写
        #   （`reports/<job_id>-<日期>.md`），只是不再从这里打开。


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
    """右侧抽屉。只管渲染与「关闭」信号，开关动画由主窗口管。

    ★ 只发 `closed` 一个信号：已读由主窗口在展开时一次性处理（打开即全读），
      存档入口已按用户要求去掉，所以没有别的回调需要往上传。
    """

    closed = pyqtSignal()

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

        # ★ 「全部已读」按钮去掉了：抽屉一打开就全部标为已读，
        #   这个按钮在同一个界面上永远没有可点的状态（点之前角标已经清零）。

        close = QToolButton()
        close.setObjectName("notifyClose")
        close.setText("×")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.closed)
        head.addWidget(close, 0)
        lay.addLayout(head)

        #: 只保留最近 N 天 —— 数字读的是存储层的常量，不在这里再写一遍，
        #: 否则改了保留期文案不会跟着变。
        self._keep_days = int(DEFAULT_KEEP_DAYS)

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

        # ★ 保留期说明放在抽屉**底部固定位置**，不随列表滚动：
        #   它的作用是解释"为什么翻不到更早的消息"，滚走了就失去意义。
        self._foot = QLabel(
            f"———— 只显示最近{self._keep_days}天的消息 ————")
        self._foot.setObjectName("notifyFootNote")
        self._foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._foot, 0)

        self._count = 0

    # ---- 渲染 ----

    def set_items(self, items: Sequence) -> None:
        """整表重建。条目数量是个位数，重建比增量维护简单，也不会漏删。"""
        while self._items.count() > 1:            # 保留末尾的 stretch
            w = self._items.takeAt(0)
            wid = w.widget()
            if wid is not None:
                # ★ 必须先 `setParent(None)` 再 `deleteLater()`：
                #   `deleteLater()` 只是排进事件队列，在循环处理之前旧的控件
                #   仍然是父控件的可见子控件，会留在**原来的位置继续绘制**。
                #   主窗口里每次刷新都重建列表，于是同一屏上出现了两份文字叠在一起。
                wid.setParent(None)
                wid.deleteLater()

        rows = list(items)
        for i, item in enumerate(rows):
            row = _NotifyItem(item, self._container)
            self._items.insertWidget(self._items.count() - 1, row)
            if i < len(rows) - 1:
                self._items.insertWidget(self._items.count() - 1, _separator())

        self._count = len(rows)
        self._stack.setCurrentIndex(1 if self._count == 0 else 0)
        self._foot.setVisible(self._count > 0)
        self._scroll.verticalScrollBar().setValue(0)

    def item_count(self) -> int:
        return self._count


class NotificationToast(QFrame):
    """启动/新消息时的一个小浮条：`有 N 条未读推送` + 最新一条标题 + 两个按钮。

    【为什么是主窗口的子控件】独立窗口（`Qt.Popup` / `Qt.Tool`）在 Windows 上会
    抢焦点，用户正在输入框里打字时突然失焦很烦；而且窗口一移动它不会跟。
    做成子控件之后，`raise_()` 就能盖在聊天区上方，位置由父窗口算。

    【位置】由调用方给：`popup_at(x, y)`。父窗口尺寸变化时调用方要重新算 ——
    浮窗自己不知道"我该贴在哪个按钮下面"。
    """

    view_requested = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notifyToast")
        self.setFixedWidth(TOAST_WIDTH)
        # ★ 不抢焦点：它只是提示，不该打断打字
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        icon = QLabel()
        icon.setPixmap(_make_bubble_icon(15).pixmap(QSize(15, 15)))
        head.addWidget(icon, 0)
        self._title = QLabel("有新的推送")
        self._title.setObjectName("toastTitle")
        head.addWidget(self._title, 1)
        lay.addLayout(head)

        self._sub = QLabel("")
        self._sub.setObjectName("toastSub")
        lay.addWidget(self._sub)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.addStretch(1)
        self._view = QPushButton("查看")
        self._view.setObjectName("toastView")
        self._view.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view.clicked.connect(self._on_view)
        row.addWidget(self._view, 0)
        self._dismiss = QPushButton("忽略")
        self._dismiss.setObjectName("toastDismiss")
        self._dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dismiss.clicked.connect(self.dismiss)
        row.addWidget(self._dismiss, 0)
        lay.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        try:                                    # 阴影不是必需的，失败就算了
            from PyQt6.QtGui import QColor as _QColor
            from PyQt6.QtWidgets import QGraphicsDropShadowEffect
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(18)
            shadow.setOffset(0, 4)
            shadow.setColor(_QColor(0, 0, 0, 46))
            self.setGraphicsEffect(shadow)
        except Exception:                                       # noqa: BLE001
            pass

        self.hide()

    # ---- 内容与显示 ----

    def set_content(self, count: int, latest_title: str) -> None:
        n = max(1, int(count))
        self._title.setText(f"有 {n} 条未读推送")
        fm = self._sub.fontMetrics()
        text = str(latest_title or "").strip() or "（无标题）"
        self._sub.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                        TOAST_WIDTH - 34))

    def popup_at(self, x: int, y: int, *, ms: int = TOAST_MS) -> None:
        """显示在指定位置，`ms` 后自动收起（0 表示不自动收）。"""
        self.adjustSize()
        self.move(max(0, int(x)), max(0, int(y)))
        self.show()
        self.raise_()
        if ms > 0:
            self._timer.start(int(ms))
        else:
            self._timer.stop()

    def dismiss(self) -> None:
        self._timer.stop()
        if self.isVisible():
            self.hide()
            self.dismissed.emit()

    def _on_view(self) -> None:
        self.dismiss()
        self.view_requested.emit()