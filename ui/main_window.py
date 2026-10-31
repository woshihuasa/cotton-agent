"""
棉花问答智能体 — PyQt6 现代 AI 对话风格界面 (v2 - 多会话侧边栏)
布局：左侧 SideBar + 右侧聊天区 (QScrollArea + InputFrame)
特性：隐藏式滚动条 · 对话气泡 · 思考过程可视化 · 流式打字机 · 多会话管理
"""

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    QThread,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QPainterPath
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dataclasses import dataclass
import re
import sys
import logging
from pathlib import Path

import markdown

log = logging.getLogger("ui")

from config import AppConfig, resource_path
from core.config_manager import ConfigManager
from core.llm_client import (DEFAULT_REASONING_EFFORT,
                            REASONING_EFFORT_LEVELS)
from core.kb_builder import plan_sync, rebuild_knowledge_base, sync_knowledge_base, verify_embedding
from core.knowledge_base import KnowledgeBase
from core.notifications import NotificationStore
from core.rag_engine import RAGEngine
from ui.notifications import (
    NOTIFY_STYLE,
    NotificationButton,
    NotificationDrawer,
    NotificationToast,
    drawer_width_for,
)
from ui.settings_dialog import SettingsDialog
from ui.sidebar import SideBar

# ── QSS 样式常量 ───────────────────────────────────────────

MAIN_STYLE = """
QMainWindow {
    background-color: #F7F7F8;
}
"""

SCROLL_AREA_STYLE = """
QScrollArea#chatArea {
    background-color: transparent;
    border: none;
}
QScrollArea#chatArea QWidget#qt_scrollarea_viewport {
    background-color: #F7F7F8;
}
QScrollArea#chatArea QScrollBar:vertical {
    width: 8px;
    background: transparent;
    border: none;
    margin: 0;
}
QScrollArea#chatArea QScrollBar::handle:vertical {
    background: rgba(0, 0, 0, 0.10);
    border-radius: 4px;
    min-height: 30px;
}
QScrollArea#chatArea QScrollBar::handle:vertical:hover,
QScrollArea#chatArea QScrollBar::handle:vertical:pressed {
    background: rgba(0, 0, 0, 0.30);
}
QScrollArea#chatArea QScrollBar::add-line:vertical,
QScrollArea#chatArea QScrollBar::sub-line:vertical {
    height: 0px;
    border: none;
}
QScrollArea#chatArea QScrollBar::add-page:vertical,
QScrollArea#chatArea QScrollBar::sub-page:vertical {
    background: transparent;
}
#bubbleContainer {
    background-color: #F7F7F8;
}
"""

BUBBLE_STYLE = """
QFrame#userBubble {
    background-color: #D8D8D8;
    border-radius: 12px;
}
QFrame#aiBubble {
    background-color: transparent;
    border: none;
}
QLabel#bubbleLabel {
    font-size: 14px;
    color: #1f1f1f;
}
/* ⚠️ 这里原本有一段 `QLabel#bubbleLabel table / th / td / code / pre` 的样式，
   已经删掉 —— 它们**从来没有生效过**：Qt 的样式表只作用于控件本身，
   不进入 QLabel 内部那份富文本文档。表格的边框与表头底色改由
   `MessageBubble._restyle_html()` 用 HTML 属性给出（见那里的注释）。 */
/* 报告链接按钮：明确的可点目标（不依赖 QLabel 的链接命中区域） */
QPushButton#reportLinkBtn {
    background-color: #EAF3FF;
    border: 1px solid #B6D6F5;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 13px;
    color: #1A73E8;
    text-align: left;
}
QPushButton#reportLinkBtn:hover {
    background-color: #DCEBFF;
    border-color: #8AB8E8;
}
QPushButton#reportLinkBtn:pressed {
    background-color: #CFE2FB;
}
"""

THINKING_PANEL_STYLE = """
QFrame#thinkingPanel {
    background-color: #F5F5F5;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
}
"""

INPUT_FRAME_STYLE = """
QFrame#inputFrame {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 15px;
}
"""

INPUT_EDIT_STYLE = """
QTextEdit#inputEdit {
    background-color: transparent;
    border: none;
    font-size: 14px;
    color: #1f1f1f;
    padding: 10px 14px 4px 14px;
}
QTextEdit#inputEdit QScrollBar:vertical {
    width: 6px;
    background: transparent;
    border: none;
}
QTextEdit#inputEdit QScrollBar::handle:vertical {
    background: transparent;
    border-radius: 3px;
}
QTextEdit#inputEdit QScrollBar::handle:vertical:hover,
QTextEdit#inputEdit QScrollBar::handle:vertical:pressed {
    background: rgba(0, 0, 0, 0.2);
}
QTextEdit#inputEdit QScrollBar::add-line:vertical,
QTextEdit#inputEdit QScrollBar::sub-line:vertical {
    height: 0px;
    border: none;
}
"""

SUBMIT_BTN_STYLE = """
QPushButton#submitBtn {
    background-color: #000000;
    border: none;
    border-radius: 12px;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
}
QPushButton#submitBtn:hover {
    background-color: #333333;
}
QPushButton#submitBtn:pressed {
    background-color: #555555;
}
QPushButton#submitBtn:disabled {
    background-color: #BBBBBB;
}
"""

CONTROL_STYLE = """
/* ── 悬停提示（tooltip）：圆角白底 + 灰边框 ──
   ★ 默认 Qt tooltip 是直角、系统配色的一块方框，和本界面不搭。
     这里统一成"白底、浅灰描边、圆角"的小卡片。
     ⚠️ 只靠 QSS 做不出那个**小尖角箭头**（要自绘 popup），
        所以这是"无箭头的圆角卡片"版本。 */
QToolTip {
    background-color: #FFFFFF;
    color: #333333;
    border: 1px solid #D0D0D0;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 12px;
}

/* ── 模型选择 / 思维链长度：同一种"胶囊"外观 ──
   ★ 2026-10-18 统一两条规则：
     · **平时**：背景透明、边框透明 —— 不占视觉重量，像纯文字；
     · **悬停/按下**：只出现底色，**边框颜色与底色完全相同** → 边框实际上看不见。
   这样两个控件在任何状态下都不会出现"描边"，视觉上是一致的。 */
QPushButton#modelBtn, QPushButton#effortBtn {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 12px;
    padding: 4px 10px;
    min-height: 20px;
    color: #333333;
    font-size: 13px;
}
QPushButton#modelBtn:hover, QPushButton#effortBtn:hover {
    background-color: #F0F0F0;
    border: 1px solid #F0F0F0;      /* 与底色同色 → 看不出描边 */
}
QPushButton#modelBtn:pressed, QPushButton#effortBtn:pressed {
    background-color: #E4E4E4;
    border: 1px solid #E4E4E4;
}

"""

TOGGLE_BTN_STYLE = """
QPushButton#toggleBtn {
    background-color: transparent;
    border: none;
    border-radius: 4px;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
}
QPushButton#toggleBtn:hover {
    background-color: #E0E0E0;
}
"""

# ── 思维链长度四档 ─────────────────────────────────────────
#
# ★ 界面档位与"发给接口的取值"**不是同一套字**：
#   实测 `reasoning_effort="off"` 会 400（`invalid_parameter_error`），
#   真正表示"关闭思考"的是 `none`。映射表在 `core/llm_client.py`（有守卫）。
#
# ★ 默认 **High**（2026-10-18 按用户要求）：默认给足思考深度。
#   但 `stream_response` 的参数默认值仍是 None —— 这样标题/摘要等
#   **内部维护**调用不会被顺带加价。
_EFFORT_LABELS: dict[str, str] = {
    "off": "Off",
    "low": "Low",
    "high": "High",
    "max": "Max",
}

#: 模型选择 / 思维链长度**共用**的菜单样式。
#:
#: ★ 勾选标记（✔）排版的坑：原先靠"在文字前加 4 个空格"做缩进，
#:   结果和菜单**自带的勾选列**叠加，把 ✔ 挤成一小团（用户反馈"被压缩"）。
#:   正确做法是**文字不带前导空格**，由下面这条 `QMenu::indicator` 明确给出
#:   勾选列宽度，`QMenu::item` 用 padding-left 让文字对齐到勾选列之后。
@dataclass(frozen=True)
class PickerEntry:
    """下拉候选表里的**一行**。

    ★ 为什么把菜单内容抽成**纯数据**：
      · 原来的实现直接往 `QMenu` 里 `addAction`，于是"内容"和"控件"绑死，
        守卫只能去翻 `menu.actions()`，既难读也测不了滚动；
      · 抽成数据后，守卫可以直接断言"列了哪些、谁不可点、谁打勾"，
        **完全不碰 Qt 事件循环**。
    """

    kind: str                      #: "sep" 分隔线 / "header" 分组标题 / "item" 可选项
    label: str = ""
    data: object = None            #: 选中后回调携带的值
    checked: bool = False


def _picker_popup_qss() -> str:
    """下拉浮层的样式（白底 + 圆角 + 细边框）。"""
    return """
    /* 底色与圆角由 paintEvent 自绘；这里只兜底设透明，
       避免 QSS 再把整块矩形涂一遍（会盖住圆角）。 */
    QFrame#pickerPopup {
        background-color: transparent;
        border: none;
    }
    QScrollArea { background: transparent; border: none; }
    QWidget#pickerBody { background: transparent; }
    /* 滚动条：细、无按钮、圆角 */
    QScrollBar:vertical {
        background: transparent; width: 8px; margin: 2px 0 2px 0;
    }
    QScrollBar::handle:vertical {
        background: #D4D8DE; border-radius: 4px; min-height: 28px;
    }
    QScrollBar::handle:vertical:hover { background: #BFC5CD; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent;
    }
    /* 可选项：像菜单项一样，悬停出浅灰底 */
    QPushButton#pickerItem {
        border: none;
        border-radius: 6px;
        background: transparent;
        color: #1f2328;
        font-size: 13px;
        text-align: left;
        padding: 6px 12px 6px 34px;
    }
    QPushButton#pickerItem:hover { background-color: #F2F3F5; }
    /* 分组标题：深灰、小字、不可点 */
    QLabel#pickerHeader {
        color: #8a919b;
        font-size: 11px;
        padding: 8px 10px 2px 12px;
        background: transparent;
    }
    QFrame#pickerSep { background: #F0F0F0; max-height: 1px; min-height: 1px; }
    """


class PickerPopup(QFrame):
    """**可滚动**的下拉候选浮层。

    【为什么不用 QMenu】
      `QMenu` 内容变长时会**把菜单撑出屏幕**，而且**不支持滚动**
      （没有滚动条、滚轮也不起作用）—— 千问一个服务商就有 20 个模型，
      再加 DeepSeek 就是二十多行，一屏根本放不下。

    【做法】
      `QFrame`（`Qt.Popup`，点外面自动关）+ 内部 `QScrollArea`。
      · 高度**按行数封顶**（默认约 8 行），超出的靠**滚轮 / 拖滚动条**看；
      · 每行是一个 `QPushButton`（悬停有底色），分组标题是不可点的 `QLabel`；
      · 打勾的行在左侧显示 SVG 勾（`ui/icons/check.svg`）。
    """

    ROW_H = 30          #: 可选项行高
    HEADER_H = 26       #: 分组标题行高
    SEP_H = 9           #: 分隔线行高
    PAD = 8             #: 上下内边距之和

    #: 底色 / 边框 —— **写死在这里**，不吃系统调色板
    BG = "#FFFFFF"
    BORDER = "#E3E5E8"

    #: 圆角半径。★ 因为本控件是**子控件**（不是顶层窗口），圆角不需要
    #: 窗口 alpha —— 四角露出的是父窗口自己的背景，所以能安全地圆角。
    RADIUS = 8

    def __init__(self, entries: list[PickerEntry], *, max_visible: int = 8,
                 parent: QWidget | None = None) -> None:
        # ★★ 关键：这是**主窗口里的普通子控件**，不是顶层窗口。
        #
        #   为什么不用 `Qt.WindowType.Popup` 顶层窗口：圆角需要窗口四角透明，
        #   而顶层窗口的透明依赖桌面合成器（DWM）给 alpha 通道。实测在部分
        #   Windows 环境下拿不到，**圆角外会渲染成黑块**。
        #
        #   作为子控件就完全没有这个问题：四角不需要 alpha，露出的是**父窗口
        #   自己的背景**（白色），所以圆角看起来是对的。
        #
        #   代价：`Qt.Popup` 的"点外面自动关闭"没有了，需要自己实现
        #   （见 `eventFilter` 与 `popup_at`）。
        super().__init__(parent)
        self.setObjectName("pickerPopup")
        # 让 QSS 的 background 真正绘制（普通 QWidget/QFrame 子类需要这个标志）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_picker_popup_qss())

        self._on_pick = None
        self._max_visible = max(1, int(max_visible))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)     # 让 1px 边框露出来
        outer.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(self._scroll)

        body = QWidget()
        body.setObjectName("pickerBody")
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(4, 4, 4, 4)
        self._body_layout.setSpacing(0)
        self._scroll.setWidget(body)

        content_h = 8        # 上下 margin
        check_icon = QIcon(str(resource_path("ui/icons/check.svg")))
        row_count = 0

        for e in entries:
            if e.kind == "sep":
                line = QFrame()
                line.setObjectName("pickerSep")
                line.setFixedHeight(1)
                self._body_layout.addSpacing(self.SEP_H // 2)
                self._body_layout.addWidget(line)
                self._body_layout.addSpacing(self.SEP_H // 2)
                content_h += self.SEP_H
            elif e.kind == "header":
                lbl = QLabel(e.label)
                lbl.setObjectName("pickerHeader")
                self._body_layout.addWidget(lbl)
                content_h += self.HEADER_H
            else:
                btn = QPushButton(e.label)
                btn.setObjectName("pickerItem")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setFixedHeight(self.ROW_H)
                btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                if e.checked and not check_icon.isNull():
                    btn.setIcon(check_icon)
                    btn.setIconSize(QSize(16, 16))
                btn.clicked.connect(
                    lambda _checked=False, data=e.data: self._pick(data))
                self._body_layout.addWidget(btn)
                content_h += self.ROW_H
                row_count += 1

        self._body_layout.addStretch(1)

        # ★★ 给内容页一个**最小高度**，等于内容真实高度。
        #   否则在"控件尚未被布局（父窗口未 show）"时，
        #   `QScrollArea` 只给 body 一个 100x30 的默认尺寸，
        #   **滚动条范围算出来是 0** —— 看着能滚，实际滚不动。
        #   实测：未 show 时 body=100x30、sb.max=0；show 之后才对。
        body.setMinimumHeight(content_h)

        # 高度：不超过 max_visible 行，也不超过内容实际高度
        cap = self.PAD + self.ROW_H * self._max_visible
        visible_h = min(content_h, cap)
        self._scroll.setFixedHeight(max(visible_h, self.ROW_H))

        # 宽度按最长文字粗估，够用即可（避免过宽遮挡聊天区）
        widest = max((len(e.label) for e in entries if e.label), default=8)
        width = min(max(180, widest * 9 + 90), 420)
        # ★ **宽和高都钉死**，而不是只钉宽：
        #   之前只 `setFixedWidth`，高度交给布局在 `adjustSize()` 时算。
        #   于是"父窗口还没 show()"时算出来的高度会塌掉 ——
        #   实测守卫里（窗口未显示）滚动条范围变成 0，而手动 show 后又是 441。
        #   尺寸自己定死，就不存在这个依赖。
        self.setFixedSize(width, visible_h + 2)      # +2 给上下 1px 边框
        self._item_rows = row_count

    # ---- 绘制 ----

    def paintEvent(self, ev) -> None:                        # noqa: N802
        """**自己画**圆角白底与边框。

        ★★ 为什么必须自绘（这是一个花了很久才定位的坑）：
          本控件是 `Qt.Popup` 顶层窗口 + `WA_TranslucentBackground`。
          这个属性在**关掉系统背景绘制**的同时，也让
          `QFrame#pickerPopup { background-color: #FFFFFF }` 这条 QSS **静默失效**
          （不报错、不告警，就是不画）。

          实测证据：在深色应用调色板下采样渲染像素，
          **整张图 0% 是白色**，四角与空白处的像素是 `#000000` 且 **alpha=0** ——
          即完全透明。透明窗口叠在深色桌面上，看起来就是"黑底 + 灰字"，
          也就是用户报告的"颜色反了"。

          所以这里手动 `fillPath` 一个圆角矩形，颜色与边框都写死，
          **不依赖调色板、不依赖 QSS 的 background**。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, float(self.RADIUS), float(self.RADIUS))

        painter.fillPath(path, QColor(self.BG))
        painter.setPen(QPen(QColor(self.BORDER), 1.0))
        painter.drawPath(path)
        painter.end()

    # ---- 交互 ----

    def set_pick_handler(self, fn) -> None:
        self._on_pick = fn

    def _pick(self, data) -> None:
        self.close()
        if self._on_pick is not None:
            self._on_pick(data)

    def popup_at(self, anchor: QWidget) -> None:
        """把浮层放到 `anchor` **上方或下方**（父控件坐标系）。

        ★ 与"窗口版"的区别：这里算的是**父控件内部坐标**，不是全局坐标 ——
          因为本控件是主窗口里的子控件，不是独立窗口。
        ★ 弹出方向：下方空间不够就**朝上翻**（输入区在窗口底部，几乎总是朝上）。
        """
        parent = self.parentWidget() or anchor.window()
        # ★ 不调 `adjustSize()`：尺寸在 `__init__` 里已 `setFixedSize` 钉死，
        #   再 adjust 一次反而会被"父窗口未显示"时的空 sizeHint 覆盖掉。
        w, h = self.width(), self.height()
        # anchor 左上角换算到父坐标系
        top_left = anchor.mapTo(parent, QPoint(0, 0))
        pw, ph = parent.width(), parent.height()

        x = top_left.x()
        x = max(4, min(x, pw - w - 4))          # 左右夹紧，别出窗口

        below = top_left.y() + anchor.height() + 4
        above = top_left.y() - h - 4
        # 下方放得下就往下，否则往上；两边都不够时选空间大的那边
        if below + h <= ph - 4:
            y = below
        elif above >= 4:
            y = above
        else:
            y = max(4, min(below, ph - h - 4))

        self.move(x, y)
        self.raise_()                            # 盖在聊天区/输入框之上
        self.show()

        # 「点外面关闭」：子控件没有 Qt.Popup 的自动关闭，这里用应用级事件过滤器补上
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:      # noqa: N802
        """点浮层以外的地方 / 按 Esc → 关闭。"""
        if not self.isVisible():
            return False
        etype = ev.type()
        if etype == QEvent.Type.MouseButtonPress:
            pos = ev.globalPosition().toPoint() if hasattr(
                ev, "globalPosition") else ev.globalPos()
            if not self.rect().contains(self.mapFromGlobal(pos)):
                self.close()
        elif etype == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
            self.close()
        return False

    def hideEvent(self, ev) -> None:             # noqa: N802
        """隐藏时摘掉事件过滤器，避免越积越多。"""
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(ev)

    #: 供测试读取（可见行数上限 / 实际可选项数）
    @property
    def max_visible(self) -> int:
        return self._max_visible

    @property
    def item_rows(self) -> int:
        return self._item_rows


def _make_arrow_icon() -> QIcon:
    """用 QPainter 绘制羽翼状白色向上箭头，生成 QIcon。"""
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(
        Qt.GlobalColor.white, 2.5,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    painter.setPen(pen)
    painter.drawLine(12, 19, 12, 5)
    painter.drawLine(12, 4, 5, 11)
    painter.drawLine(12, 4, 19, 11)
    painter.end()
    return QIcon(pixmap)


def _make_hamburger_icon() -> QIcon:
    """绘制汉堡菜单（三横线）图标。"""
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(
        QColor("#666666"), 2.0,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
    )
    painter.setPen(pen)
    painter.drawLine(2, 4, 14, 4)
    painter.drawLine(2, 8, 14, 8)
    painter.drawLine(2, 12, 14, 12)
    painter.end()
    return QIcon(pixmap)


# ── 可旋转箭头控件 ──────────────────────────────────────


class ArrowToggle(QWidget):
    """空心 > 箭头，点击时平滑顺时针旋转 90° 变为 ∨。"""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rotation = 0.0         # 0° = > (收起)，90° = ∨ (展开)
        self._is_expanded = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(16, 16)

        self._anim = QPropertyAnimation(self, b"_dummy")
        self._anim.setDuration(150)

    def set_expanded(self, expanded: bool) -> None:
        """触发旋转动画：True → ∨ (90°)，False → > (0°)。"""
        if self._is_expanded == expanded:
            return
        self._is_expanded = expanded
        self._rotation = 90.0 if expanded else 0.0
        self.update()

    def paintEvent(self, event) -> None:
        """绘制 > 形。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen = QPen(
            QColor("#888888"), 2.0,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self._rotation)

        d = 4
        path = QPainterPath()
        path.moveTo(-d / 2, -d)
        path.lineTo(d / 2, 0)
        path.lineTo(-d / 2, d)
        painter.drawPath(path)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self.clicked.emit()


# ── 图片预览对话框（高清查看 + 保存）────────────────────


def _save_pixmap(pixmap: QPixmap, parent) -> bool:
    """通用保存对话框：选择路径后保存 PNG。返回是否保存成功。"""
    path, _ = QFileDialog.getSaveFileName(
        parent, "保存图片", "棉花趋势图.png", "PNG 图片 (*.png)",
    )
    if not path:
        return False
    ok = pixmap.save(path, "PNG")
    if ok:
        QMessageBox.information(parent, "保存成功", f"图片已保存到:\n{path}")
    else:
        QMessageBox.warning(parent, "保存失败", "图片保存失败，请检查路径或权限。")
    return ok


class ImageViewerDialog(QDialog):
    """高清图片预览弹窗：全尺寸显示（可滚动），支持保存到本地。"""

    def __init__(self, pixmap: QPixmap, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"图片预览 — {title}")
        self.resize(960, 680)
        self._pixmap = pixmap

        layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        img_label = QLabel()
        img_label.setPixmap(pixmap)  # 全尺寸原图，超出区域由滚动条承载
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(img_label)
        layout.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        save_btn = QPushButton("保存图片")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._on_save)
        close_btn = QPushButton("关闭")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _on_save(self) -> None:
        """将原图保存到用户选择的位置。"""
        _save_pixmap(self._pixmap, self)


class ClickableImageLabel(QLabel):
    """可点击图片标签：左键弹出高清预览，右键弹出保存菜单。"""

    # 白色圆角无边框菜单样式（与侧边栏 ··· 菜单一致）
    _MENU_QSS = """
        QMenu {
            background-color: #FFFFFF;
            border: 1px solid #F0F0F0;
            border-radius: 10px;
            padding: 4px 0px;
        }
        QMenu::item {
            padding: 6px 24px;
            font-size: 13px;
            color: #333333;
        }
        QMenu::item:selected {
            background-color: #F5F5F5;
            border-radius: 4px;
        }
    """

    def __init__(self, shown_pixmap: QPixmap, full_pixmap: QPixmap,
                 title: str, parent=None) -> None:
        super().__init__(parent)
        self._full_pixmap = full_pixmap
        self._img_title = title
        self.setPixmap(shown_pixmap)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("左键查看高清大图，右键保存图片")

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            dlg = ImageViewerDialog(self._full_pixmap, self._img_title, self.window())
            dlg.exec()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        """右键弹出「保存图片」菜单（白色圆角无边框）。"""
        menu = QMenu(self)
        # 去除原生窗口边框与系统级方形阴影，实现圆角弹窗
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        menu.setStyleSheet(self._MENU_QSS)

        save_action = menu.addAction("保存图片")
        save_action.triggered.connect(self._save_image)
        menu.exec(event.globalPos())

    def _save_image(self) -> None:
        """保存全尺寸原图。"""
        _save_pixmap(self._full_pixmap, self.window())


# ── 消息气泡组件 ──────────────────────────────────────────


class MessageBubble(QWidget):
    """单条消息气泡组件。

    AI 气泡额外支持可折叠的「思考过程」面板，
    置于回答气泡上方，与气泡共用同一宽度约束。
    """

    #: 气泡里的链接被点击（参数是 href 原文）。
    #:
    #: ★ 为什么不直接用 `QLabel.setOpenExternalLinks(True)`：
    #   那会把 href **原样**交给系统打开 —— 而 href 来自模型输出，
    #   模型完全可能写出 `file:///C:/Windows/System32/...`。
    #   所以这里只**转出信号**，由 `MainWindow` 做**白名单校验**后再决定是否打开。
    link_clicked = pyqtSignal(str)

    _freeze_layout: bool = False  # 布局冻结开关，动画期间阻止 resize 重算

    def __init__(self, text: str, is_user: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._is_user = is_user

        # ── 文本标签 ──
        self._label = QLabel(self._escape_html(text))
        self._label.setObjectName("bubbleLabel")
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.TextFormat.RichText)
        if not is_user:
            self._label.setTextInteractionFlags(
                self._link_flags())
        else:
            self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # ★ 把 QLabel 的 linkActivated 转成气泡自己的信号（外部做白名单校验）
        self._label.linkActivated.connect(self.link_clicked)
        self._label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )

        # ── 回答气泡框 ──
        self._bubble = QFrame()
        self._bubble.setObjectName("userBubble" if is_user else "aiBubble")
        self._bubble.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )

        bubble_layout = QVBoxLayout(self._bubble)
        bubble_layout.addWidget(self._label)
        m = 14 if is_user else 10
        bubble_layout.setContentsMargins(m, m, m, m)

        # ── 内容列（思考面板 + 气泡，共享宽度）──
        self._column_layout = QVBoxLayout()
        self._column_layout.setContentsMargins(0, 0, 0, 0)
        self._column_layout.setSpacing(6)
        self._column_layout.addWidget(self._bubble)

        self._content_column = QWidget()
        self._content_column.setLayout(self._column_layout)

        # ── 对齐行 ──
        alignment_row = QWidget()
        alignment = QHBoxLayout(alignment_row)
        alignment.setContentsMargins(0, 0, 0, 0)
        if is_user:
            alignment.addStretch(1)
            alignment.addWidget(self._content_column, 0)
        else:
            alignment.addWidget(self._content_column, 0)
            alignment.addStretch(1)

        # ── 主布局 ──
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(alignment_row)

        # ── 思考面板状态（仅 AI 使用，延迟创建）──
        self._thinking_panel: QFrame | None = None
        self._toggle_btn: ArrowToggle | None = None
        self._thinking_header_label: QLabel | None = None
        self._thinking_content: QLabel | None = None
        self._thinking_collapsed = True
        self._thinking_finished = False

        # ── 图片段（![alt](path.png) 渲染结果）──
        self._image_labels: list[QLabel] = []
        self._rendered_images: set[str] = set()

    # ── 公开接口 ──

    def update_text(self, text: str) -> None:
        """替换气泡全部文本。"""
        self._label.setText(self._escape_html(text))
        self._relayout()
        self._adjust_bubble_width()

    def append_text(self, chunk: str) -> None:
        """追加文本到回答气泡末尾（流式 tween）。"""
        current = self._label.text()
        self._label.setText(current + self._escape_html(chunk))
        self._adjust_bubble_width()

    def text(self) -> str:
        """返回当前回答文本（含 HTML 转义）。"""
        return self._label.text()

    def set_think_style(self, thinking: bool) -> None:
        """切换回答标签灰色 / 正常样式。"""
        if thinking:
            self._label.setStyleSheet("color: #888888;")
        else:
            self._label.setStyleSheet("")

    def set_streaming(self, streaming: bool) -> None:
        """流式输出期间禁止划词，结束后允许并渲染 Markdown。"""
        if self._is_user:
            return
        if streaming:
            self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        else:
            # 提取 ![图表](路径) 图片标记并渲染图片段
            raw = self._label.text()
            if raw:
                raw = self._render_images(raw)
                # ★ 报告链接 → 真按钮（比 Markdown 链接可靠）
                raw = self._render_report_links(raw)
                # 将剩余的 Markdown 文本转为 HTML 重新渲染
                html = self._render_markdown(raw)
                self._label.setText(html)
            # ★ 结束后要**允许点链接**（否则 Markdown 渲染出的 <a> 点不动）
            self._label.setTextInteractionFlags(self._link_flags())

    @staticmethod
    def _link_flags() -> Qt.TextInteractionFlag:
        """可划词 **+ 可点链接** 的交互标志。

        ★ 缺一不可：只给 `TextSelectableByMouse` 时，`<a href>` 会被渲染成
          普通彩色文字 —— **点了没反应**，而且不会报任何错。
        """
        return (Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.LinksAccessibleByMouse)


    @staticmethod
    def _render_markdown(text: str) -> str:
        """将 Markdown 文本转为 HTML。"""
        html = markdown.markdown(text, extensions=['tables', 'fenced_code'])
        return MessageBubble._restyle_html(html)

    @staticmethod
    def _restyle_html(html: str) -> str:
        """把 Markdown 转出来的 HTML 调成聊天里能看的样子。

        ★ 这两处都是「**Qt 样式表管不到文档内部**」的直接后果，不是审美问题：

          ① **标题**。Qt 给 `<h1>` 的默认字号约 2em，模型一写 `#`，就是一行
             30px 的大标题，整段回答立刻从"回答"变成"文档"。而
             `QLabel#bubbleLabel { font-size: 14px }` 对它无效 —— 那条规则只
             作用于控件本身。所以只能在这里改成段落 + 内联字号。
          ② **表格**。`QLabel#bubbleLabel th, td { border: 1px solid #DDD }`
             这类规则**从来没有生效过**，表格一直显示成一排没有框线的列。
             边框、内边距、表头底色只能用 HTML 属性给。

        ★ 保留 `<p>`/`<strong>`/`<li>` 等结构，只换标题与表格的呈现方式 ——
          数据、措辞、层级一个字都不动。
        """
        html = re.sub(
            r"<h1>(.*?)</h1>",
            r'<p style="font-size: 16px; font-weight: 600; '
            r'margin: 14px 0 6px 0;">\1</p>',
            html, flags=re.S)
        html = re.sub(
            r"<h([2-6])>(.*?)</h\1>",
            r'<p style="font-size: 15px; font-weight: 600; '
            r'margin: 12px 0 6px 0;">\2</p>',
            html, flags=re.S)
        html = html.replace(
            "<table>",
            '<table border="1" cellspacing="0" cellpadding="6" width="100%">')
        html = html.replace("<th>", '<th bgcolor="#F2F2F4">')
        return html

    # ── 图片段渲染 ──

    def _render_images(self, raw_text: str) -> str:
        """提取文本中的 ![alt](xxx.png) 图片标记并渲染为气泡内图片段。

        返回移除图片标记后的剩余文本（交给 Markdown 渲染）。
        同一路径只渲染一次，避免 set_streaming(False) 重复调用时重复插图。
        """
        pattern = re.compile(r"!\[[^\]]*\]\(([^)]+\.png)\)")
        for m in pattern.finditer(raw_text):
            path = m.group(1).strip()
            if path not in self._rendered_images:
                self._rendered_images.add(path)
                self._append_image(path)
        return pattern.sub("", raw_text)

    def _render_report_links(self, raw_text: str) -> str:
        """把 `[📄 打开报告](路径)` 渲染成**真按钮**，返回移除该标记后的文本。

        ★ 为什么不依赖 Markdown 链接（`<a href>`）：
          QLabel 的链接需要**精确命中锚文本**才触发 `linkActivated`，
          实测在真实界面上很容易"点了没反应"，且没有任何视觉反馈
          （光标形状、下划线都取决于 QSS）。
          一个 `QPushButton` 是**明确的可点目标**，且点击必然触发 ——
          对"用户要把报告打开"这件事，可靠性比样式重要。

        ★ 仍然走同一个白名单：按钮只把 href 交给 `link_clicked`，
          由 `MainWindow._on_link_clicked()` 校验后再打开。
        """
        pattern = re.compile(r"\[([^\]]*打开报告[^\]]*)\]\(([^)]+)\)")
        found = list(pattern.finditer(raw_text))
        for m in found:
            label = (m.group(1) or "打开报告").strip()
            href = m.group(2).strip()
            btn = QPushButton(f"📄 {label}" if "📄" not in label else label)
            btn.setObjectName("reportLinkBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setToolTip(href)
            # ★ 用默认参数绑定 href，避免闭包晚绑定
            btn.clicked.connect(lambda _=False, h=href: self.link_clicked.emit(h))
            self._image_labels.append(btn)
            self._bubble.layout().addWidget(btn)
            self._bubble.adjustSize()
            self.updateGeometry()
        return pattern.sub("", raw_text)

    def _append_image(self, path: str) -> None:
        """加载图片并追加到气泡布局；等比缩放显示，点击可查看高清原图。"""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        full = QPixmap(str(candidate)) if candidate.exists() else QPixmap()
        if full.isNull():
            label = QLabel(f"[图片加载失败: {path}]")
            label.setStyleSheet("color: #999999; font-size: 12px;")
        else:
            max_w = 480  # 气泡内显示宽度上限（源图为 150dpi，点击可看全尺寸）
            shown = full
            if full.width() > max_w:
                shown = full.scaledToWidth(
                    max_w, Qt.TransformationMode.SmoothTransformation
                )
            label = ClickableImageLabel(shown, full, candidate.name)
        label.setWordWrap(False)
        self._image_labels.append(label)
        self._bubble.layout().addWidget(label)
        self._bubble.adjustSize()
        self.updateGeometry()

    # ── 思考面板 ──

    def _ensure_thinking_panel(self) -> None:
        """延迟创建思考面板（首次收到 reasoning 时）。"""
        if self._thinking_panel is not None:
            return

        panel = QFrame()
        panel.setObjectName("thinkingPanel")

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 6, 10, 6)
        panel_layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)

        self._thinking_header_label = QLabel("正在深度思考中...")
        self._thinking_header_label.setStyleSheet(
            "color: #888888; font-size: 13px; font-weight: bold; background: transparent;"
        )
        header.addWidget(self._thinking_header_label)

        self._toggle_btn = ArrowToggle()
        self._toggle_btn.clicked.connect(self._toggle_thinking)
        header.addWidget(self._toggle_btn)
        header.addStretch()
        panel_layout.addLayout(header)

        self._thinking_content = QLabel()
        self._thinking_content.setWordWrap(True)
        self._thinking_content.setTextFormat(Qt.TextFormat.RichText)
        self._thinking_content.setStyleSheet(
            "color: #666666; font-size: 13px; line-height: 1.5; background: transparent;"
        )
        self._thinking_content.setVisible(False)
        panel_layout.addWidget(self._thinking_content)

        self._column_layout.insertWidget(0, panel)
        self._thinking_panel = panel

    def update_thinking(self, text: str) -> None:
        """追加推理文本到思考面板。"""
        self._ensure_thinking_panel()
        current = self._thinking_content.text()
        self._thinking_content.setText(current + self._escape_html(text))

    def finish_thinking(self) -> None:
        """标记思考结束，更新头部标题。"""
        if self._thinking_header_label is not None:
            self._thinking_header_label.setText("思考过程")
        self._thinking_finished = True

    def _toggle_thinking(self) -> None:
        """切换思考面板展开 / 收起，触发箭头旋转动画。"""
        self._thinking_collapsed = not self._thinking_collapsed
        if self._toggle_btn is not None:
            self._toggle_btn.set_expanded(not self._thinking_collapsed)
        if self._thinking_content is not None:
            self._thinking_content.setVisible(not self._thinking_collapsed)

    # ── 内部 ──

    @staticmethod
    def _escape_html(text: str) -> str:
        """转义 HTML 特殊字符。"""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _relayout(self) -> None:
        """文本更新后强制重新布局，确保行高正确。"""
        self._label.adjustSize()
        self._bubble.adjustSize()
        self.updateGeometry()

    def _adjust_bubble_width(self) -> None:
        """根据文本固有宽度与可用空间，动态调整气泡宽度。

        文字较短 → setFixedWidth 精确匹配文字宽度，关闭换行。
        文字较长 → setMaximumWidth 到 75%/85% 上限，开启自动换行。
        """
        parent_width = self.parent().width() if self.parent() else self.width()
        if parent_width <= 0:
            return

        padding = 28 if self._is_user else 20
        max_width = int(parent_width * (0.75 if self._is_user else 0.85))
        # ★ 中文长文要给行宽设上限：窗口拉宽之后 85% 等于没有上限，
        #   一行四十多字读起来很累。760 像素大约是一行 35 字。
        if not self._is_user:
            max_width = min(max_width, 760)

        fm = QFontMetrics(self._label.font())
        inherent = fm.horizontalAdvance(self._label.text())
        desired = inherent + padding

        if desired < max_width:
            self._content_column.setMinimumWidth(desired)
            self._content_column.setMaximumWidth(desired)
            self._label.setWordWrap(False)
        else:
            self._content_column.setMinimumWidth(0)
            self._content_column.setMaximumWidth(max_width)
            self._label.setWordWrap(True)

    def resizeEvent(self, event) -> None:
        """窗口大小变化时重新计算气泡最佳宽度。"""
        if MessageBubble._freeze_layout:
            return
        super().resizeEvent(event)
        self._adjust_bubble_width()


# ── 流式工作线程 ───────────────────────────────────────────


class KBWorker(QThread):
    """知识库后台构建线程（不卡 UI）。"""

    status_signal = pyqtSignal(bool, str)  # (成功, 信息)

    def run(self) -> None:
        ok, msg = rebuild_knowledge_base()
        self.status_signal.emit(ok, msg)


class KbSyncWorker(QThread):
    """知识库增量同步线程（启动时自动更新，不卡 UI）。

    与 KBWorker 的区别：优先**增量同步**（只处理发生变化的文件），
    仅当嵌入模型变更或首次建立时，才在内部回退为全量重建。
    """

    status_signal = pyqtSignal(bool, str)  # (成功, 信息)

    def run(self) -> None:
        ok, msg = sync_knowledge_base()
        self.status_signal.emit(ok, msg)


class UpdateWorker(QThread):
    """GitHub 更新检查后台线程。"""

    result_signal = pyqtSignal(object)  # dict | None

    def run(self) -> None:
        from core.updater import check_for_update

        self.result_signal.emit(check_for_update())


class DownloadWorker(QThread):
    """更新包下载线程（带进度）。"""

    progress_signal = pyqtSignal(float)  # 0.0-1.0
    done_signal = pyqtSignal(object)     # (ok, path_or_error)

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    def run(self) -> None:
        try:
            from core.updater import download_asset

            path = download_asset(self._url, progress_cb=self.progress_signal.emit)
            self.done_signal.emit((True, str(path)))
        except Exception as e:
            self.done_signal.emit((False, str(e)))


class RAGWorker(QThread):
    """后台流式工作线程。"""
    new_text_signal = pyqtSignal(str, str)
    finished_signal = pyqtSignal()
    #: ★ 本轮 token 用量（回答结束后发一次）—— 供输入框下方显示缓存命中率
    usage_signal = pyqtSignal(dict)

    def __init__(
        self,
        engine: RAGEngine,
        question: str,
        model_name: str | None = None,
        enable_thinking: bool = False,
        provider: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._question = question
        self._model_name = model_name
        self._enable_thinking = enable_thinking
        self._provider = provider
        self._reasoning_effort = reasoning_effort

    def run(self) -> None:
        try:
            for msg_type, chunk in self._engine.ask(
                self._question,
                model_name=self._model_name,
                enable_thinking=self._enable_thinking,
                provider=self._provider,
                reasoning_effort=self._reasoning_effort,
            ):
                self.new_text_signal.emit(msg_type, chunk)
        except Exception as e:
            self.new_text_signal.emit("content", f"❌ 处理失败：{e}")
            self.new_text_signal.emit("error", "")
        finally:
            # 先把用量发出去，再报"结束" —— 顺序反了的话界面收到结束时还没数据
            try:
                u = getattr(self._engine.llm, "last_usage", None)
                if u is not None:
                    self.usage_signal.emit({
                        # ★ 刻意**同时**给出原始字段：`cache_hit_rate` 在"无数据"时
                        # 也返回 0.0，光看比率**分不清"没数据"和"真的 0%"**
                        "hit": int(getattr(u, "cache_hit_tokens", 0) or 0),
                        "miss": int(getattr(u, "cache_miss_tokens", 0) or 0),
                        "prompt": int(getattr(u, "prompt_tokens", 0) or 0),
                        "cache_hit_rate": float(getattr(u, "cache_hit_rate", 0.0) or 0.0),
                        "model": str(getattr(self._engine.llm, "_model", "") or ""),
                    })
            except Exception:                                # noqa: BLE001
                pass                                         # 用量统计绝不该影响回答
            self.finished_signal.emit()


# ── 主窗口 ─────────────────────────────────────────────────


class CottonAgentWindow(QMainWindow):
    """棉花种植决策智能体主窗口 (v2 - 多会话侧边栏)。

    布局：左 SideBar | 右 (聊天区 + 输入框)
    """

    #: 定时触发层的消息（`core/schedule.py`）。
    #:
    #: ★ 它来自**后台线程**（调度线程等任务跑完的那个），而 Qt 控件只能在主线程碰。
    #:   所以调度层只发一个 dict，经这个信号排队转到主线程再动界面 ——
    #:   直接在后台线程里建气泡会让程序偶发崩溃且难以复现。
    schedule_push = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        # matplotlib 主线程预热：避免图表工具在 RAGWorker 线程首次渲染时崩溃（STATUS_IN_PAGE_ERROR）
        try:
            from core.trend_plot import warmup
            warmup()
        except Exception as e:
            print(f"[UI] matplotlib 预热失败（不影响启动）: {e}")
        print("正在初始化 RAG 引擎（首次运行需加载 Embedding 模型，请稍候）...")
        self._engine = RAGEngine()
        print("RAG 引擎初始化完成。")

        self._streaming = False
        self._current_ai_bubble: MessageBubble | None = None
        self._thinking_placeholder = "正在思考中..."

        # 流式打字效果
        self._pending_chunks = ""
        self._type_interval = 30
        self._type_speed = 3
        self._update_timer = QTimer()
        self._update_timer.setSingleShot(False)
        self._update_timer.setInterval(self._type_interval)
        self._update_timer.timeout.connect(self._flush_pending_text)

        # 确保存在当前会话
        if not self._engine.current_session_id:
            self._engine.create_session()

        # 收件箱要在建界面之前就绪：`_init_ui` 里的角标与抽屉第一次刷新要读它
        self._notify_store = NotificationStore()

        self._init_ui()
        self._apply_styles()
        self._refresh_sidebar()
        self._load_history()
        self._refresh_notifications()

        # 延迟启动知识库自动检测（窗口先显示，避免启动阻塞）
        QTimer.singleShot(300, self._ensure_knowledge_base)
        # 延迟启动更新检查
        QTimer.singleShot(2000, self._check_for_update)
        # 定时触发层（每日农情 / 极端天气预警）。
        # ★ 排在知识库检测之后：调度要用知识库，得先让它有机会准备好；
        #   而知识库是惰性取的（`kb_provider`），所以这时刻只是"大概率已就绪"。
        self._scheduler = None
        self.schedule_push.connect(self._on_schedule_notice)
        QTimer.singleShot(5000, self._start_scheduler)

        # 更新下载完成后退出时拉起 updater.exe
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._on_about_to_quit)
        self._pending_update_zip: str | None = None

    # ── 知识库自动构建与维护 ────────────────────────────

    def _ensure_knowledge_base(self, force_embedding_check: bool = False) -> None:
        """启动后自动检测并更新知识库：

        1. Embedding API 未配置/不可用 → 跳过（降级为数据工具模式）
        2. 与 data/ 一致 → 就绪（静默，不打扰用户）
        3. 文档有变化 → 后台**增量同步**（状态栏提示，通常几秒）
        4. 首次建立 / 嵌入模型变更 → 后台**全量重建**（状态栏提示约 1 分钟）
        """
        ok, msg = verify_embedding(force=force_embedding_check)
        if not ok:
            print(f"[KB] 知识库未构建：{msg}")
            log.info("知识库未构建：%s", msg)
            return

        # 事前规划：据此显示不同文案，让用户能区分"几秒"与"约一分钟"
        plan = plan_sync()
        mode = plan.get("mode", "none")
        if mode == "none":
            log.info("知识库已就绪：%s", plan.get("reason"))
            return

        if mode == "incremental":
            changed = plan["added"] + plan["updated"] + plan["removed"]
            self._set_status(f"正在更新知识库（{changed} 篇内容变化）…")
            log.info("检测到文档变化，启动增量同步：%s", plan)
        elif plan.get("reason") == "首次建立":
            self._set_status("正在准备知识库（首次使用，约需 1 分钟）…")
            log.info("首次建立知识库：%s", plan)
        else:
            self._set_status("正在升级知识库（本次更新包含检索模型变更）…")
            log.info("嵌入模型变更，启动全量重建：%s", plan)

        self._kb_worker = KbSyncWorker()
        self._kb_worker.status_signal.connect(self._on_kb_build_finished)
        self._kb_worker.start()

    def _on_kb_build_finished(self, ok: bool, msg: str) -> None:
        """构建/同步线程完成回调（主线程）：刷新知识库引用或降级提示。"""
        if ok:
            try:
                self._engine.kb = KnowledgeBase()  # 刷新为已构建的库
            except Exception as e:
                print(f"[KB] 刷新知识库失败: {e}")
                log.warning("刷新知识库失败: %s", e)
            print(f"[KB] {msg}")
            log.info("知识库处理完成：%s", msg)
            self._set_status(msg)      # 完成后消息常驻，便于用户确认结果
        else:
            print(f"[KB] {msg}")
            log.warning("知识库处理失败：%s", msg)
            self._set_status("知识库更新失败（不影响其他功能）")
            QMessageBox.warning(
                self,
                "知识库更新失败",
                f"{msg}\n\n已降级为数据工具问答模式，产量/价格/图表等本地工具仍可用。"
                "修复配置后重启应用将自动重试。",
            )

    # ── 定时触发层：每日农情 / 极端天气预警 ─────────────────

    def _start_scheduler(self) -> None:
        """启动 `core/schedule.py` 的调度线程。

        ★ 失败只打日志、不弹窗：一个跑不起来的定时任务不该妨碍用户问问题。
        """
        if self._scheduler is not None:
            return
        from core import schedule as sched
        try:
            self._scheduler = sched.Scheduler(
                # 惰性取知识库：启动那一刻库可能还在同步，取到的 None 会被
                # 记成「库不可用」，那是一次假失败。
                kb_provider=lambda: getattr(self._engine, "kb", None),
                on_event=self._on_schedule_event,
            )
            self._scheduler.start()
        except Exception as e:                                # noqa: BLE001
            print(f"[Schedule] 启动失败（不影响对话）: {e}")
            log.warning("定时触发层启动失败: %s", e)
            self._scheduler = None
            return
        log.info("定时触发层已启动：%s", [j.id for j in self._scheduler.jobs])
        print(f"[Schedule] 已启动 {len(self._scheduler.jobs)} 个触发项")

    def _on_schedule_event(self, payload: dict) -> None:
        """调度层回调（**后台线程**）—— 只转成信号，界面的事交给主线程。

        ★ 这里**不能**碰任何控件（也不能格式化后塞进聊天区）：
          它跑在调度线程或 `_watch` 线程里，直接动 QLabel 会偶发崩溃。
        """
        if not isinstance(payload, dict):
            return
        self.schedule_push.emit(payload)

    def _on_schedule_notice(self, payload: dict) -> None:
        """收到调度消息（**主线程**）：推送进收件箱，其余进状态栏。

        ★ 推送**不再进聊天区**。它是客户端级的，进了对话就等于绑死在当前会话上：
          换会话看不到、被 L2 压缩压掉、空会话里还会触发标题生成。
        """
        kind = str(payload.get("type") or "")
        text = str(payload.get("text") or "").strip()
        if not text:
            return

        if kind != "notice":
            # 失败 / 降级 / 跳过：如实写在状态栏，不包装成「正常」。
            # 这类消息由触发层按「不消费计划时刻」重试，不该每天刷一屏气泡。
            self._set_status(f"定时任务：{text}")
            return

        try:
            self._notify_store.add(
                job_id=str(payload.get("job") or ""),
                title=str(payload.get("title") or ""),
                body=text,
                path=str(payload.get("path") or ""),
            )
        except ValueError as e:
            log.warning("推送没能落进收件箱：%s", e)
            self._set_status("收到一条推送，但落库失败（详见日志）")
            return

        self._refresh_notifications()
        self._set_status("收到一条新推送")
        # 抽屉没开就弹一下浮条；开着就不用弹，用户已经在看了
        self._maybe_show_toast()

    # ── 通知中心：角标、抽屉、已读 ─────────────────────────

    def _refresh_notifications(self) -> None:
        """按收件箱的当前状态刷新角标与抽屉内容。"""
        self._notify_btn.set_unread(self._notify_store.unread_count())
        self._notify_drawer.set_items(self._notify_store.items())

    def _toggle_notify_drawer(self) -> None:
        if self._notify_open:
            self._close_notify_drawer()
        else:
            self._open_notify_drawer()

    def _open_notify_drawer(self) -> None:
        """展开抽屉。**打开即全部已读**（已定的口径）。"""
        if self._notify_open:
            return
        self._notify_open = True
        # ★ 抽屉一开，浮条就该收起来：它本来就是"点我去看"的入口，
        #   用户已经点开了，还压在列表上会挡住内容。
        self._notify_toast.dismiss()
        self._notify_drawer.setVisible(True)
        self._refresh_notifications()
        if self._notify_store.mark_all_read():
            self._notify_btn.set_unread(self._notify_store.unread_count())
        self._animate_notify(0, drawer_width_for(self.height()))

    def _close_notify_drawer(self) -> None:
        if not self._notify_open:
            return
        self._notify_open = False
        self._animate_notify(self._notify_drawer.width(), 0)

    def _animate_notify(self, start: int, end: int) -> None:
        """同时动 `minimumWidth` 与 `maximumWidth`。

        ★ 只动 `maximumWidth` 拉不宽：抽屉的 `sizeHint` 比目标宽度小，
          布局按 `sizeHint` 给宽度，上限放宽了也不会变（实测只到 170）。
          把 min 与 max 一起推到同一个值，宽度才真的跟动画走。
        """
        self._notify_anim.stop()
        for anim in (self._notify_anim_w, self._notify_anim_n):
            anim.setStartValue(int(start))
            anim.setEndValue(int(end))
        self._notify_anim.start()

    def _on_notify_anim_finished(self) -> None:
        """收起动画结束后隐藏抽屉，免得它继续占着布局的焦点与命中区域。"""
        if not self._notify_open and self._notify_drawer.maximumWidth() <= 0:
            self._notify_drawer.setVisible(False)

    # ── 启动浮窗 ────────────────────────────────────────

    def showEvent(self, event) -> None:                       # noqa: N802
        """第一次显示之后才弹浮窗。

        ★ 必须等窗口真的显示了：`show()` 之前布局还没走到位，
          `mapTo()` 算出来的坐标是错的（入口按钮会在左上角）。
          第一次显示之后，最小化再恢复也不再弹，免得反复打扰。
        """
        super().showEvent(event)
        if not getattr(self, "_toast_armed", False):
            self._toast_armed = True
            QTimer.singleShot(900, self._maybe_show_toast)

    def _toast_position(self) -> tuple[int, int]:
        """浮窗锚在入口按钮下方，并夹在窗口内。"""
        toast = self._notify_toast
        toast.adjustSize()
        anchor = self._notify_btn.mapTo(self, QPoint(0, self._notify_btn.height()))
        x = anchor.x() + self._notify_btn.width() - toast.width()
        y = anchor.y() + 6
        x = max(8, min(x, self.width() - toast.width() - 8))
        y = max(8, min(y, self.height() - toast.height() - 8))
        return x, y

    def _maybe_show_toast(self) -> None:
        """有未读就弹一下；抽屉开着的时候不弹（用户已经在看了）。"""
        if not self.isVisible() or getattr(self, "_notify_open", False):
            return
        count = self._notify_store.unread_count()
        if count <= 0:
            return
        items = self._notify_store.items()
        self._notify_toast.set_content(count, items[0].title if items else "")
        x, y = self._toast_position()
        self._notify_toast.popup_at(x, y)

    def resizeEvent(self, event) -> None:                     # noqa: N802
        """窗口尺寸变化时，抽屉宽度按 9:16 重算，浮窗跟着入口按钮走。"""
        super().resizeEvent(event)
        if getattr(self, "_notify_open", False):
            self._notify_drawer.setMaximumWidth(drawer_width_for(self.height()))
        toast = getattr(self, "_notify_toast", None)
        if toast is not None and toast.isVisible():
            x, y = self._toast_position()
            toast.move(x, y)

    # ── 软件更新检查与安装 ──────────────────────────────

    def _check_for_update(self) -> None:
        """启动后检查 GitHub 是否有新版本（后台线程，异常静默）。"""
        if not (AppConfig.GITHUB_OWNER.strip() and AppConfig.GITHUB_REPO.strip()):
            print("[Update] 未配置 GitHub 仓库，跳过更新检查。")
            log.info("未配置 GitHub 仓库，跳过更新检查。")
            return
        self._update_worker = UpdateWorker()
        self._update_worker.result_signal.connect(self._on_update_checked)
        self._update_worker.start()

    def _on_update_checked(self, info) -> None:
        """检查结果回调：有新版则询问用户。"""
        if not info:
            print("[Update] 当前已是最新版本（或检查失败）。")
            log.info("更新检查：当前已是最新版本（或检查失败）。")
            return
        print(f"[Update] 发现新版本: {info['version']}")
        log.info("发现新版本: %s", info["version"])
        ret = QMessageBox.question(
            self,
            "发现新版本",
            f"发现新版本 {info['version']}\n\n更新说明:\n{info.get('notes', '无')}\n\n"
            "是否立即下载并更新？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self._download_update(info["asset_url"])

    def _download_update(self, url: str) -> None:
        """下载更新包（带进度对话框）。"""
        self._progress = QProgressDialog("正在下载更新...", "取消", 0, 100, self)
        self._progress.setWindowTitle("软件更新")
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.setMinimumWidth(360)

        self._dl_worker = DownloadWorker(url)
        self._dl_worker.progress_signal.connect(
            lambda p: self._progress.setValue(int(p * 100))
        )
        self._dl_worker.done_signal.connect(self._on_download_done)
        self._dl_worker.start()

    def _on_download_done(self, result) -> None:
        """下载完成：提示重启安装，退出时拉起 updater.exe。"""
        ok, payload = result
        if not ok:
            QMessageBox.warning(self, "下载失败", f"更新包下载失败:\n{payload}")
            return
        self._pending_update_zip = payload
        QMessageBox.information(
            self,
            "更新已下载",
            "更新包已下载完成。点击确定后将退出应用并自动完成安装，安装后自动重启。",
        )
        self.close()

    def _on_about_to_quit(self) -> None:
        """退出前拉起 updater.exe 完成安装（仅打包模式）。"""
        if not self._pending_update_zip:
            return
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            updater = exe_dir / "updater.exe"
            if updater.exists():
                import subprocess

                subprocess.Popen([
                    str(updater), "--staging", self._pending_update_zip,
                    "--install", str(exe_dir), "--app", "CottonAgent.exe",
                ])
                print("[Update] 已拉起 updater.exe，应用退出后自动安装。")
                log.info("已拉起 updater.exe，应用退出后自动安装。")
                return
        print(f"[Update] 更新包已保存: {self._pending_update_zip}（开发模式，请手动安装）")
        log.info("更新包已保存: %s（开发模式，请手动安装）", self._pending_update_zip)

    # ── 样式 ─────────────────────────────────────────────

    def _apply_styles(self) -> None:
        """应用全局与组件级 QSS 样式。"""
        self.setStyleSheet(
            MAIN_STYLE + SCROLL_AREA_STYLE + BUBBLE_STYLE + THINKING_PANEL_STYLE
            + INPUT_FRAME_STYLE + INPUT_EDIT_STYLE + SUBMIT_BTN_STYLE
            + TOGGLE_BTN_STYLE + CONTROL_STYLE + NOTIFY_STYLE
        )

    # ── UI 构建 ──────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        """设置底栏左侧的常驻信息（知识库维护进度等）。

        ★ 替代原来的 `self.statusBar().showMessage(...)`。
          把它从 `QMainWindow.statusBar()` 搬进聊天容器内部，是为了
          ① 侧边栏能顶到窗口底部；② 不与右侧缓存命中抢位置；③ 去掉那条分割线。
        """
        lbl = getattr(self, "_status_msg", None)
        if lbl is not None:
            lbl.setText(msg or "")

    def _init_ui(self) -> None:
        """构建整体布局：侧边栏 | 聊天区。"""
        self.setWindowTitle("棉花种植决策智能体")
        self.resize(900, 600)
        self.setMinimumSize(600, 400)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── 左侧边栏 ──
        self._sidebar = SideBar()
        self._sidebar.session_selected.connect(self._on_session_selected)
        self._sidebar.new_session_requested.connect(self._on_new_session)
        self._sidebar.settings_requested.connect(self._on_settings)
        self._sidebar.session_rename_requested.connect(self._on_rename_session)
        self._sidebar.session_delete_requested.connect(self._on_delete_session)
        root_layout.addWidget(self._sidebar)

        # ── 右侧聊天区 ──
        chat_container = QWidget()
        chat_layout = QVBoxLayout(chat_container)
        chat_layout.setContentsMargins(8, 4, 8, 8)
        chat_layout.setSpacing(6)

        # ── 顶部工具栏：侧边栏切换按钮 ──
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 0)

        self._toggle_btn = QPushButton()
        self._toggle_btn.setObjectName("toggleBtn")
        self._toggle_btn.setIcon(_make_hamburger_icon())
        self._toggle_btn.setToolTip("展开/收起侧边栏")
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle_sidebar)
        toolbar.addWidget(self._toggle_btn)
        toolbar.addStretch()

        # ── 右侧通知入口：角标显示未读数 ──
        self._notify_btn = NotificationButton()
        self._notify_btn.clicked.connect(self._toggle_notify_drawer)
        toolbar.addWidget(self._notify_btn)
        chat_layout.addLayout(toolbar)

        # ── 对话气泡区 ──
        self._scroll_area = QScrollArea()
        self._scroll_area.setObjectName("chatArea")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._bubble_container = QWidget()
        self._bubble_container.setObjectName("bubbleContainer")
        self._bubble_layout = QVBoxLayout(self._bubble_container)
        self._bubble_layout.setContentsMargins(0, 0, 0, 0)
        self._bubble_layout.setSpacing(4)

        self._bottom_spacer = QSpacerItem(
            20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding
        )
        self._bubble_layout.addItem(self._bottom_spacer)

        self._scroll_area.setWidget(self._bubble_container)
        chat_layout.addWidget(self._scroll_area, stretch=1)

        # ── 输入容器 ──
        self._input_frame = QFrame()
        self._input_frame.setObjectName("inputFrame")
        self._input_frame.setFixedHeight(100)
        chat_layout.addWidget(self._input_frame)

        # ── 底栏：知识库维护信息（左）+ 缓存命中（右），**同一个容器** ──
        #
        # ★ 2026-10-18 第二次改造，三件事一起解决：
        #
        #   ① 原来用 `QMainWindow.statusBar()`，它是**横跨整个窗口**的独立控件，
        #      于是侧边栏被压在它上方、**够不到窗口底部**（用户反馈）。
        #   ② 缓存命中作为 permanent widget 钉在右侧，与 `showMessage()` 的临时消息
        #      **抢同一块地方**，知识库维护信息被挤掉（用户反馈"被遮住"）。
        #   ③ 状态栏自带一条上边框分割线，视觉上把窗口切成两截。
        #
        # 所以不再用 `QMainWindow.statusBar()`，改在**聊天容器内部**放一行：
        #   · 它只占右侧聊天区，**侧边栏因此能顶到窗口底部**；
        #   · 左右各一个 QLabel，**永不互相遮挡**；
        #   · 无边框、无背景，视觉上是聊天区的自然收尾。
        status_row = QHBoxLayout()
        status_row.setContentsMargins(8, 0, 6, 2)
        status_row.setSpacing(10)

        self._status_msg = QLabel("")
        self._status_msg.setObjectName("statusMsg")
        self._status_msg.setStyleSheet(
            "color:#8a919b; font-size:11px; background:transparent;")
        status_row.addWidget(self._status_msg, 1)

        # 显示原则不变：**"没有数据"和"数据是零"必须区分** ——
        # 初始就显示「缓存命中 —」，而不是留空（留空会让人以为坏了）。
        self._status_cache = QLabel("缓存命中 —")
        self._status_cache.setObjectName("statusCache")
        self._status_cache.setStyleSheet(
            "color:#8a919b; font-size:11px; background:transparent;")
        self._status_cache.setToolTip("上一轮问答的提示词缓存命中情况")
        status_row.addWidget(self._status_cache, 0)

        chat_layout.addLayout(status_row)

        frame_layout = QVBoxLayout(self._input_frame)
        frame_layout.setContentsMargins(4, 4, 4, 6)
        frame_layout.setSpacing(0)

        self._input_edit = QTextEdit()
        self._input_edit.setObjectName("inputEdit")
        self._input_edit.setPlaceholderText("请输入...")
        self._input_edit.installEventFilter(self)
        frame_layout.addWidget(self._input_edit)

        input_toolbar = QHBoxLayout()
        input_toolbar.setContentsMargins(6, 2, 6, 2)

        # ── 初始模型：从服务商登记表读，**不再写死 deepseek-v4-flash** ──
        # ★ `_provider_name` 刻意**留空**：空表示"用引擎自己的客户端"。
        #   只有这样，web 模式下用户自带的 API key 才不会被我在这里
        #   换成服务器凭据层里的 key。只有用户**主动从菜单选了**才填它。
        self._model_name = ""
        self._provider_name = ""
        _default_label = "deepseek-v4-flash"
        try:
            from core import providers as _prov
            _pname = _prov.default_provider()
            if _pname:
                _spec = _prov.all_specs().get(_pname) or {}
                _default_label = str(_spec.get("default_model") or _default_label)
        except Exception:                                    # noqa: BLE001
            pass

        self._model_btn = QPushButton(_default_label)
        self._model_btn.setObjectName("modelBtn")
        # ★ 2026-10-18 去掉右侧的小三角：它由 SVG 渲染且 viewBox 是扁的（64x38），
        #   缩到 16px 后形状不清晰，反而显得粗糙。按钮本身的"可点"感由 hover 底色承担。
        self._model_btn.setStyleSheet(
            self._model_btn.styleSheet() + "text-align: left; padding-left: 12px;"
        )
        self._model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_btn.setToolTip("选择 AI 模型")
        # ★ 去掉点击后文字周围的**虚线焦点框**。
        #   顺带一个好处：点它不会把键盘焦点从输入框抢走，可以点完继续打字。
        self._model_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._model_btn.setStyleSheet(
            self._model_btn.styleSheet() + "outline: none;")
        self._model_btn.clicked.connect(self._show_model_menu)
        input_toolbar.addWidget(self._model_btn)
        input_toolbar.addSpacing(8)
        # ── 思考长度：与「模型选择」**同构**的胶囊按钮 + 菜单 ──
        # ★ 2026-10-18 第三次改：先试过 QComboBox，但它自带系统下拉箭头与方框，
        #   怎么套样式都不像模型选择那一颗。所以直接**复用同一套控件组合**
        #   （QPushButton + QMenu），外观与交互天然一致。
        # ★ 另一个原因同上次：不传思考参数时服务端**默认仍在思考**，
        #   所以 "Off" 必须显式发 `reasoning_effort=none` 才算真的关。
        self._effort_level = DEFAULT_REASONING_EFFORT
        self._effort_btn = QPushButton(_EFFORT_LABELS[self._effort_level])
        self._effort_btn.setObjectName("effortBtn")
        # ★ 同模型按钮：去掉小三角（理由见上）
        self._effort_btn.setStyleSheet(
            self._effort_btn.styleSheet() + "text-align: left; padding-left: 12px;")
        self._effort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._effort_btn.setToolTip("思维链长度")
        self._effort_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._effort_btn.clicked.connect(self._show_effort_menu)
        input_toolbar.addWidget(self._effort_btn)

        input_toolbar.addStretch()

        self._submit_btn = QPushButton()
        self._submit_btn.setObjectName("submitBtn")
        self._submit_btn.setIcon(_make_arrow_icon())
        self._submit_btn.setIconSize(self._submit_btn.iconSize())
        self._submit_btn.setToolTip("发送 (Ctrl+Enter)")
        self._submit_btn.clicked.connect(self._on_submit)
        input_toolbar.addWidget(self._submit_btn)

        frame_layout.addLayout(input_toolbar)

        root_layout.addWidget(chat_container, stretch=1)

        # ── 右侧通知抽屉（默认收起，宽度动画与左边栏同一套做法）──
        #
        # ★ 为什么让它**参与布局**而不是浮在上面：浮层会盖住工具栏右端的入口按钮，
        #   于是"再点一次收起"就点不到了。参与布局的代价是聊天区会跟着窄一点，
        #   但这正是左边栏一直以来的行为，两者一致。
        self._notify_drawer = NotificationDrawer()
        self._notify_drawer.setMaximumWidth(0)
        self._notify_drawer.setMinimumWidth(0)
        self._notify_drawer.setVisible(False)
        self._notify_drawer.closed.connect(self._close_notify_drawer)
        # ★ 抽屉只发「关闭」一个信号：已读在展开时一次性处理（打开即全读），
        #   所以既没有「全部已读」按钮要接，也没有存档入口要接。
        root_layout.addWidget(self._notify_drawer)

        # 启动/新消息时的小浮条。★ 是窗口的**子控件**，不是独立窗口 ——
        # 独立窗口会抢焦点，用户正在打字时突然失焦很烦，且窗口一移动它不跟。
        self._notify_toast = NotificationToast(self)
        self._notify_toast.view_requested.connect(self._open_notify_drawer)
        self._toast_armed = False

        self._notify_anim = QParallelAnimationGroup(self)
        self._notify_anim_w = QPropertyAnimation(self._notify_drawer, b"maximumWidth")
        self._notify_anim_n = QPropertyAnimation(self._notify_drawer, b"minimumWidth")
        for _a in (self._notify_anim_w, self._notify_anim_n):
            _a.setDuration(180)
            _a.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._notify_anim.addAnimation(_a)
        self._notify_anim.finished.connect(self._on_notify_anim_finished)
        self._notify_open = False

        # 侧边栏动画
        self._sidebar_anim = QPropertyAnimation(self._sidebar, b"maximumWidth")
        self._sidebar_anim.setDuration(200)
        self._sidebar_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._sidebar_anim.finished.connect(self._on_sidebar_anim_finished)
        self._sidebar_visible = True
        self._sidebar_width = 260

    # ── 侧边栏控制 ──────────────────────────────────────

    def _toggle_sidebar(self) -> None:
        """展开/收起侧边栏，使用宽度动画。"""
        self._sidebar_anim.stop()
        # 核心优化：冻结所有气泡的内部布局计算
        MessageBubble._freeze_layout = True
        self._scroll_area.setUpdatesEnabled(False)
        if self._sidebar_visible:
            self._sidebar_anim.setStartValue(self._sidebar.width())
            self._sidebar_anim.setEndValue(0)
            self._sidebar_visible = False
        else:
            self._sidebar_anim.setStartValue(self._sidebar.width())
            self._sidebar_anim.setEndValue(self._sidebar_width)
            self._sidebar_visible = True
        self._sidebar_anim.start()

    def _on_sidebar_anim_finished(self) -> None:
        """动画结束，解冻聊天区并强制刷新布局。"""
        self._scroll_area.setUpdatesEnabled(True)
        # 解冻气泡，统一刷新一次宽度
        MessageBubble._freeze_layout = False
        self._bubble_container.updateGeometry()
        for i in range(self._bubble_layout.count()):
            widget = self._bubble_layout.itemAt(i).widget()
            if widget and isinstance(widget, MessageBubble):
                widget._adjust_bubble_width()

    # ── 滚动控制 ────────────────────────────────────────

    def _scroll_to_bottom(self) -> None:
        sb = self._scroll_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _add_bubble(self, text: str, is_user: bool) -> MessageBubble:
        """创建气泡并插入到底部弹性空间之前。"""
        bubble = MessageBubble(text, is_user, self._bubble_container)
        # ★ 气泡里的链接 → 走白名单校验后再打开（见 _on_link_clicked）
        bubble.link_clicked.connect(self._on_link_clicked)
        idx = self._bubble_layout.count() - 1
        self._bubble_layout.insertWidget(idx, bubble)
        return bubble

    # ── 报告链接：白名单校验后打开 ─────────────────────────

    #: 允许从聊天里打开的**目录**（相对项目根）。
    #:
    #: ★ 为什么必须有白名单：气泡里的 href 来自**模型输出**
    #:   （或我们拼接的路径），而 `QDesktopServices.openUrl` 会把它
    #:   原样交给系统 —— 模型完全可能写出 `file:///C:/Windows/...`。
    #:   所以只允许打开**这两个产物目录下**的已知类型文件。
    _OPENABLE_DIRS: tuple[str, ...] = ("reports", "charts")
    #: 允许打开的文件类型
    _OPENABLE_SUFFIXES: tuple[str, ...] = (
        ".md", ".txt", ".png", ".jpg", ".jpeg", ".csv", ".json")

    def _on_link_clicked(self, href: str) -> None:
        """处理气泡里的链接点击。**校验不过就什么都不做，并给出原因。**"""
        target, reason = self._check_openable(href)
        if target is None:
            # ★ 把**真实原因**和**解析结果**都摆出来，别让用户猜
            QMessageBox.warning(
                self, "无法打开",
                f"{reason}\n\n"
                f"你点的链接是：\n{href}")
            print(f"[Report] ★ 拒绝打开：{reason} | href={href!r}")
            return
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            # ★ 兜底：系统没有 .md 的默认程序时，至少让人能打开所在文件夹。
            #   "打不开"不能是死路 —— 报告就在那儿，得给用户一个可执行的下一步。
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("无法直接打开")
            box.setText("系统没有能打开这个文件的程序。\n\n" + str(target))
            folder_btn = box.addButton("打开所在文件夹",
                                       QMessageBox.ButtonRole.ActionRole)
            box.addButton("知道了", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is folder_btn:
                QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(target.parent)))

    @classmethod
    def _resolve_openable(cls, href: str) -> Path | None:
        """把 href 解析成**允许打开**的绝对路径；不允许则返回 `None`。

        ★ 纯函数（不碰 UI），便于守卫直接测。
        """
        return cls._check_openable(href)[0]

    @classmethod
    def _check_openable(cls, href: str) -> tuple[Path | None, str]:
        """同 `_resolve_openable`，但**同时返回拒绝原因**。

        ★ 为什么要原因：弹窗原本只说"只允许 reports/charts 下…"，
          而实际拒绝可能是**文件不存在**、**扩展名不对**或**路径穿越** ——
          用户看到的提示和真实原因对不上，排查全靠猜。
        """
        if not href:
            return None, "链接为空"
        raw = str(href).strip()
        # http(s) 等外部链接**一律不从这里打开**（要开请用户自己复制）
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
            return None, "外部链接（http/https/file 等协议）一律不在应用内打开"
        p = Path(raw)
        try:
            resolved = (p if p.is_absolute() else Path.cwd() / p).resolve()
        except (OSError, RuntimeError) as e:
            return None, f"路径无法解析：{e}"
        if not resolved.is_file():
            return None, f"文件不存在（可能已被清理）：\n{resolved}"
        if resolved.suffix.lower() not in cls._OPENABLE_SUFFIXES:
            return None, (f"文件类型 {resolved.suffix!r} 不在允许列表里："
                          f"{'、'.join(cls._OPENABLE_SUFFIXES)}")
        # ★ 必须落在白名单目录**之下**（用 resolve 后的真实路径判断，
        #   这样 `reports/../../etc` 之类会被挡住）
        #
        # ★★ 基准必须是**项目根**而不是 cwd（2026-10-20 修）：
        #   `RAGEngine._report_dir()` 现在返回项目根下的绝对路径，
        #   如果这里还用 cwd 作根，从别处启动的应用就会**两边对不上**
        #   —— 报告写到了项目里，白名单却在 cwd 里找。
        root = Path(__file__).resolve().parent.parent
        for d in cls._OPENABLE_DIRS:
            allowed = (root / d).resolve()
            try:
                resolved.relative_to(allowed)
            except ValueError:
                continue
            return resolved, ""
        return None, (f"不在允许打开的目录下。\n\n"
                      f"只允许：{' / '.join(cls._OPENABLE_DIRS)}\n"
                      f"项目根：{root}\n"
                      f"实际位置：{resolved}\n\n"
                      f"★ 若这是临时目录，说明产物没有落在项目内 —— "
                      f"请重启应用后重试。")

    def _clear_bubbles(self) -> None:
        """清空所有气泡（保留底部 spacer）。"""
        while self._bubble_layout.count() > 1:
            item = self._bubble_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ── 会话历史加载 ────────────────────────────────────

    def _load_history(self) -> None:
        """清空气泡区，根据当前会话的 L2 历史重新渲染。"""
        self._clear_bubbles()

        sid = self._engine.current_session_id
        if not sid or sid not in self._engine.sessions:
            return

        session = self._engine.sessions[sid]
        for msg in session.conversation_store:
            is_user = (msg["role"] == "user")
            bubble = self._add_bubble(msg["content"], is_user=is_user)
            if not is_user:
                if msg.get("reasoning"):
                    bubble.update_thinking(msg["reasoning"])
                    bubble.finish_thinking()
                bubble.set_streaming(False)  # 触发 Markdown 渲染

        QTimer.singleShot(0, self._scroll_to_bottom)

    # ── 侧边栏刷新 ──────────────────────────────────────

    def _refresh_sidebar(self) -> None:
        """从引擎拉取会话列表并刷新侧边栏。"""
        sessions = self._engine.get_session_list()
        self._sidebar.refresh_list(sessions)

    # ── 事件过滤：Ctrl+Enter 快捷键 ──────────────────────

    def eventFilter(self, obj, event) -> bool:
        if obj is self._input_edit and event.type() == event.Type.KeyPress:
            if (event.key() == Qt.Key.Key_Return
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._on_submit()
                return True
        return super().eventFilter(obj, event)

    # ── 交互逻辑 ─────────────────────────────────────────

    def _on_session_selected(self, session_id: str) -> None:
        """侧边栏选中会话 → 切换并加载历史。"""
        if self._streaming:
            return  # 流式输出中禁止切换
        if session_id == self._engine.current_session_id:
            return
        if self._engine.switch_session(session_id):
            self._sidebar.set_current_item(session_id)
            self._load_history()

    def _on_new_session(self) -> None:
        """新建会话 → 清空界面。"""
        if self._streaming:
            return
        sid = self._engine.create_session()
        self._sidebar.add_session_item(sid, "新对话")
        self._clear_bubbles()
        # 聚焦输入框
        self._input_edit.setFocus()

    def _on_settings(self) -> None:
        """弹出设置对话框，允许修改 API Key。"""
        dialog = SettingsDialog(self)
        if dialog.exec():
            print("[UI] 配置已更新。")
            # 刷新内存中的配置（.env 已写入），使新 key 立即生效
            from config import reload_config

            reload_config()
            # 强制重新验证 Embedding 并触发知识库构建检测
            self._ensure_knowledge_base(force_embedding_check=True)
            QMessageBox.information(
                self,
                "设置已保存",
                "配置已更新。部分设置（如 Embedding 模型）可能需要重启应用后生效。",
            )

    def _on_rename_session(self, session_id: str) -> None:
        """重命名会话。"""
        session = self._engine.sessions.get(session_id)
        if session is None:
            return
        new_title, ok = QInputDialog.getText(
            self, "重命名会话", "请输入新名称:", text=session.title,
        )
        if ok and new_title.strip():
            self._engine.rename_session(session_id, new_title.strip())
            self._sidebar.update_session_title(session_id, new_title.strip())
            self._refresh_sidebar()

    def _on_delete_session(self, session_id: str) -> None:
        """删除会话。"""
        session = self._engine.sessions.get(session_id)
        if session is None:
            return
        reply = QMessageBox.question(
            self, "删除会话",
            f"确定删除会话「{session.title}」吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        was_current = (session_id == self._engine.current_session_id)
        self._engine.delete_session(session_id)
        self._sidebar.remove_session_item(session_id)
        self._refresh_sidebar()
        if was_current:
            self._sidebar.set_current_item(self._engine.current_session_id)
            self._load_history()

    # ── 候选表：**纯数据**（便于零成本测试）────────────────

    def _effort_entries(self) -> list[PickerEntry]:
        """思维链长度的四项候选。"""
        return [
            PickerEntry("item", _EFFORT_LABELS[lv], data=lv,
                        checked=(lv == self._effort_level))
            for lv in REASONING_EFFORT_LEVELS
        ]

    def _model_entries(self) -> list[PickerEntry]:
        """模型候选表 —— 按**已配置密钥的服务商**自动生成。

        【为什么当初要重做】原来是写死的
        `for model in ["deepseek-v4-flash", "deepseek-v4-pro"]`，
        配好千问之后下拉框里还是 DeepSeek 的两个模型。

        【结构】每个服务商一段：**分组标题**（不可点）+ 它供应的**模型**（可点）；
        服务商之间用分隔线隔开。默认服务商排最前。
        """
        from core import credentials, providers

        try:
            specs = providers.all_specs()
        except Exception as e:                                   # noqa: BLE001
            specs = {}
            print(f"[模型候选表] 读取服务商失败：{e}")

        enabled: list[tuple[str, dict]] = []
        for name, spec in specs.items():
            try:
                has = credentials.has_key(name)
            except Exception:                                    # noqa: BLE001
                has = False
            if has:
                enabled.append((name, spec))

        if not enabled:
            return [
                PickerEntry("header", "尚未配置任何服务商密钥"),
                PickerEntry("item", "去「设置 → 模型」配置", data=("__settings__", "")),
            ]

        try:
            cur_prov = providers.default_provider()
        except Exception:                                        # noqa: BLE001
            cur_prov = ""
        enabled.sort(key=lambda kv: (kv[0] != cur_prov, kv[1].get("label", "")))

        entries: list[PickerEntry] = []
        for idx, (name, spec) in enumerate(enabled):
            if idx:
                entries.append(PickerEntry("sep"))
            entries.append(PickerEntry("header", str(spec.get("label") or name)))
            models = spec.get("models") or [spec.get("default_model")]
            # ★ 过滤**非对话模型**（语音 / 图像 / 视频）—— 登记表是完整目录，
            #   但候选表里选到它们只会在调用时报错，不该出现在这里。
            _non_chat = getattr(providers, "NON_CHAT_MODELS", frozenset())
            for m in models:
                if not m or m in _non_chat:
                    continue
                entries.append(PickerEntry(
                    "item", str(m), data=(name, str(m)),
                    checked=(str(m) == self._model_name
                             and name == self._provider_name)))
        return entries

    # ── 弹出 ─────────────────────────────────────────────

    def _show_picker(self, entries: list[PickerEntry], anchor: QWidget,
                     on_pick, *, max_visible: int = 8) -> PickerPopup:
        """在 `anchor` 下方弹出候选浮层（超过约 8 行可滚动）。"""
        # ★ 挂到 `centralWidget()` 而不是 QMainWindow 本身：
        #   QMainWindow 的被管理布局会忽略直接子控件的几何，浮层会定位错乱。
        host = self.centralWidget() or self
        popup = PickerPopup(entries, max_visible=max_visible, parent=host)
        popup.set_pick_handler(on_pick)
        popup.popup_at(anchor)
        return popup

    def _show_effort_menu(self) -> None:
        """弹出思维链长度候选（四档，不需要滚动，但复用同一套浮层）。"""
        self._show_picker(self._effort_entries(), self._effort_btn,
                          self._on_effort_selected, max_visible=4)

    def _show_model_menu(self) -> None:
        """弹出模型候选（服务商分组，超过约 8 行可滚动）。"""
        self._show_picker(self._model_entries(), self._model_btn,
                          self._on_model_picked, max_visible=8)

    def _on_model_picked(self, data) -> None:
        """候选浮层回调：把 `(provider, model)` 原样交给选中逻辑。"""
        if not data:
            return
        prov, model = data
        if prov == "__settings__":
            self._on_settings()
            return
        self._on_model_selected(str(model), str(prov))

    def _on_effort_selected(self, level: str) -> None:
        """选中某档思考长度。"""
        if level not in _EFFORT_LABELS:
            return
        self._effort_level = level
        self._effort_btn.setText(_EFFORT_LABELS[level])

    def _on_model_selected(self, model: str, provider: str = "") -> None:
        """选中某个模型 —— **同时记住它属于哪个服务商**。

        ★ 只记模型名是不够的：HTTP 客户端的 base_url / API key 在构造时就定了，
          所以必须把 provider 一起带到 `ask()`，否则会拿默认服务商的端点
          去请求另一家的模型名。
        """
        self._model_name = model
        self._provider_name = provider
        self._model_btn.setText(model)
        tooltip = f"{model}"
        try:
            from core import providers as _p
            spec = _p.all_specs().get(provider) or {}
            if spec.get("label"):
                tooltip = f"{spec['label']} · {model}"
        except Exception:                                        # noqa: BLE001
            pass
        self._model_btn.setToolTip(tooltip)
    def _on_submit(self) -> None:
        """用户提交问题。"""
        if self._streaming:
            return  # 正在输出中，忽略

        question = self._input_edit.toPlainText().strip()
        if not question:
            return

        # 确保当前会话存在
        if not self._engine.current_session_id:
            self._engine.create_session()
            self._refresh_sidebar()

        model_name = self._model_name or self._model_btn.text()
        # ★ 思考长度四档：Off/Low/High/Max（Off 会显式发 reasoning_effort=none）
        # ★ 2026-10-20 修：这里原本读 `_effort_combo.currentData()`，
        #   但"思维链长度"已从**下拉框**改成**按钮 + 弹层**，
        #   属性也随之变成 `_effort_level`（等级字符串）。
        #   漏改的那一处会让**任何输入**都抛 AttributeError ——
        #   用户在界面上一条消息都发不出去。
        reasoning_effort = str(self._effort_level or "off")
        enable_thinking = reasoning_effort != "off"
        provider = self._provider_name

        self._add_bubble(question, is_user=True)

        if enable_thinking:
            self._thinking_placeholder = "正在深度思考中，请稍候..."
        else:
            self._thinking_placeholder = "正在思考中..."

        self._current_ai_bubble = self._add_bubble(
            self._thinking_placeholder, is_user=False
        )
        self._current_ai_bubble.set_streaming(True)
        self._current_ai_bubble.set_think_style(True)

        self._submit_btn.setEnabled(False)
        self._input_edit.clear()
        self._streaming = True

        QTimer.singleShot(0, self._scroll_to_bottom)

        self._worker = RAGWorker(
            self._engine, question,
            model_name=model_name,
            enable_thinking=enable_thinking,
            provider=provider,
            reasoning_effort=reasoning_effort,
        )
        self._worker.new_text_signal.connect(self._on_update_text)
        self._worker.finished_signal.connect(self._on_stream_finished)
        self._worker.usage_signal.connect(self._on_usage)
        self._worker.start()

    def _on_usage(self, d: dict) -> None:
        """更新**底栏**的缓存命中率提示。

        ★ 判"有没有数据"与文案生成都在 `core.prompt_layers.format_cache_hint()` 里 ——
        那是**纯函数**，能被零成本守卫覆盖；这里只负责贴到 QLabel。
        """
        lbl = getattr(self, "_status_cache", None)
        if lbl is None:
            return
        from core.prompt_layers import format_cache_hint
        text, tip = format_cache_hint(
            int(d.get("hit", 0) or 0), int(d.get("miss", 0) or 0),
            model=str(d.get("model") or ""),
        )
        lbl.setText(text)
        lbl.setToolTip(tip)

    def _on_update_text(self, msg_type: str, chunk: str) -> None:
        """接收流式 (类型, 片段) —— 分支处理。"""
        if self._current_ai_bubble is None:
            return

        if msg_type == "reasoning":
            self._current_ai_bubble.update_thinking(chunk)
            QTimer.singleShot(0, self._scroll_to_bottom)
            return

        # ── 回答内容 ──
        current = self._current_ai_bubble.text()
        if current == MessageBubble._escape_html(self._thinking_placeholder):
            self._current_ai_bubble.finish_thinking()
            self._current_ai_bubble.set_think_style(False)
            self._current_ai_bubble.update_text(chunk)
            self._bubble_container.updateGeometry()
            QTimer.singleShot(0, self._scroll_to_bottom)
            return

        self._pending_chunks += chunk
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _flush_pending_text(self) -> None:
        """定时器触发：每 tick 从缓冲区弹 _type_speed 个字符到回答气泡。"""
        if not self._pending_chunks or self._current_ai_bubble is None:
            return

        take = min(self._type_speed, len(self._pending_chunks))
        self._current_ai_bubble.append_text(self._pending_chunks[:take])
        self._pending_chunks = self._pending_chunks[take:]

        self._bubble_container.updateGeometry()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _on_stream_finished(self) -> None:
        """流式结束：停定时器，清空缓冲区，刷新侧边栏。"""
        self._update_timer.stop()
        if self._pending_chunks and self._current_ai_bubble is not None:
            self._current_ai_bubble.append_text(self._pending_chunks)
            self._pending_chunks = ""
            self._bubble_container.updateGeometry()
            QTimer.singleShot(0, self._scroll_to_bottom)

        self._streaming = False
        if self._current_ai_bubble:
            self._current_ai_bubble.set_streaming(False)
        self._current_ai_bubble = None
        self._submit_btn.setEnabled(True)

        # 刷新侧边栏（标题可能已更新）
        self._refresh_sidebar()

    # ── 窗口关闭 ─────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """关闭前停止定时器与调度线程。"""
        self._update_timer.stop()
        sched = getattr(self, "_scheduler", None)
        if sched is not None:
            try:
                sched.stop()
            except Exception:                                # noqa: BLE001
                pass
        super().closeEvent(event)
