"""
多会话侧边栏组件

现代扁平化风格，包含:
  - 顶部"新建对话"按钮
  - 主体会话列表 (QListWidget + SessionItemWidget)
  - 底部设置按钮
  - 每项右侧 ··· 按钮支持重命名/删除
"""

from PyQt6.QtCore import pyqtSignal, Qt, QSize
from PyQt6.QtGui import QAction, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ── 样式常量 ───────────────────────────────────────────

SIDEBAR_STYLE = """
#sidebar {
    background-color: #F0F0F0;
    border-right: 1px solid #E0E0E0;
}
#newBtn {
    background-color: transparent;
    border: none;
    border-radius: 8px;
    text-align: left;
    padding-left: 12px;
    color: #333333;
    font-weight: bold;
    font-size: 13px;
}
#newBtn:hover {
    background-color: #E0E0E0;
}
#newBtn:pressed {
    background-color: #D0D0D0;
}
#sessionList {
    background-color: transparent;
    border: none;
    font-size: 13px;
    color: #333333;
    outline: none;
}
#sessionList::item {
    padding: 0px;
    border-radius: 6px;
    margin: 2px 6px;
}
#sessionList::item:hover {
    background-color: #E8E8E8;
    color: #333333;
}
#sessionList::item:selected {
    background-color: #E0E0E0;
    color: #333333;
}
QScrollBar:vertical {
    width: 6px;
    background: transparent;
    border: none;
}
QScrollBar::handle:vertical {
    background: #C0C0C0;
    border-radius: 3px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #A0A0A0;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
    border: none;
}
#settingsBtn {
    background-color: transparent;
    border: none;
    border-radius: 8px;
    font-size: 16px;
}
#settingsBtn:hover {
    background-color: #E0E0E0;
}
QToolTip {
    background-color: #FFFFFF;
    color: #666666;
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}
"""

# ── 工具函数 ──────────────────────────────────────────

def _make_plus_icon() -> QPixmap:
    """绘制 16px 灰色 + 图标。"""
    size = 16
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(Qt.GlobalColor.gray, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    cx = size // 2
    painter.drawLine(cx, 4, cx, size - 4)
    painter.drawLine(4, cx, size - 4, cx)
    painter.end()
    return pixmap


# ── 会话列表项控件 ────────────────────────────────────

class SessionItemWidget(QWidget):
    """单个会话列表项的自定义控件。

    左侧标题，右侧 ··· 按钮（默认隐藏，hover 或选中时显示）。
    """

    clicked = pyqtSignal(str)
    rename_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    def __init__(
        self, session_id: str, title: str, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_id = session_id
        self._is_selected = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 6, 6)
        layout.setSpacing(6)

        # 标题
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet("background: transparent;")
        layout.addWidget(self._title_label, stretch=1)

        # ··· 按钮
        self._more_btn = QToolButton()
        self._more_btn.setText("···")
        self._more_btn.setToolTip("更多操作")
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.setFixedSize(24, 24)
        self._more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more_btn.setVisible(False)
        self._more_btn.setStyleSheet("""
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
                color: #999999;
                font-size: 14px;
                font-weight: bold;
            }
            QToolButton:hover {
                background: #E0E0E0;
                color: #666666;
            }
            QToolButton::menu-indicator {
                image: none;
            }
        """)

        # 右键菜单
        menu = QMenu(self)

        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        menu.setStyleSheet("""
            QMenu {
                background-color: rgba(255, 255, 255, 0.95);
                border: 1px solid #FFFFFF;
                border-radius: 8px;
                padding: 4px 0px;
            }
            QMenu::item {
                padding: 6px 24px;
                font-size: 13px;
                color: #333333;
                margin: 0px 4px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #F0F0F0;
                color: #000000;
            }
            QMenu::separator {
                height: 1px;
                background: #EBEBEB;
                margin: 4px 8px;
            }
            QMenu::right-arrow, QMenu::tear-off {
                image: none;
            }
        """)

        rename_action = QAction("重命名", menu)
        rename_action.triggered.connect(
            lambda: self.rename_requested.emit(self._session_id)
        )
        menu.addAction(rename_action)

        delete_action = QAction("删除", menu)
        delete_action.triggered.connect(
            lambda: self.delete_requested.emit(self._session_id)
        )
        menu.addAction(delete_action)

        self._more_btn.setMenu(menu)
        layout.addWidget(self._more_btn)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ── 公开接口 ──

    def session_id(self) -> str:
        return self._session_id

    def set_title(self, title: str) -> None:
        self._title_label.setText(title)

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self._more_btn.setVisible(selected)

    # ── 事件 ──

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._more_btn.setVisible(True)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        if not self._is_selected:
            self._more_btn.setVisible(False)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.clicked.emit(self._session_id)

    def sizeHint(self) -> QSize:
        return QSize(200, 36)


# ── 侧边栏 ────────────────────────────────────────────

class SideBar(QWidget):
    """可折叠侧边栏，展示会话列表并提供新建/切换/设置入口。"""

    session_selected = pyqtSignal(str)
    new_session_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    session_rename_requested = pyqtSignal(str)
    session_delete_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setStyleSheet(SIDEBAR_STYLE)
        self.setMinimumWidth(0)
        self.setMaximumWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(8)

        # ── 新建对话按钮 ──
        self._new_btn = QPushButton("  +  新建对话")
        self._new_btn.setObjectName("newBtn")
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.setStyleSheet("QPushButton { text-align: left; padding-left: 12px; }")
        self._new_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._new_btn.setFixedHeight(36)
        self._new_btn.clicked.connect(self.new_session_requested.emit)
        layout.addWidget(self._new_btn)

        # ── 会话列表 ──
        self._session_list = QListWidget()
        self._session_list.setObjectName("sessionList")
        self._session_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self._session_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._session_list.setWordWrap(False)
        self._session_list.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self._session_list, stretch=1)

        # ── 底部设置按钮（右对齐）──
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 4, 0)
        bottom_row.addStretch()
        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setObjectName("settingsBtn")
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.setFixedSize(32, 32)
        self._settings_btn.setToolTip("设置")
        self._settings_btn.clicked.connect(self.settings_requested.emit)
        bottom_row.addWidget(self._settings_btn)
        layout.addLayout(bottom_row)

        self._session_widgets: dict[str, SessionItemWidget] = {}
        self._updating_selection = False

    # ── 公开接口 ──

    def refresh_list(self, sessions: list[dict]) -> None:
        self._session_list.blockSignals(True)
        self._session_list.clear()
        self._session_widgets.clear()

        current_id = ""
        for s in sessions:
            sid = s["id"]
            count = s.get("message_count", 0)
            label = f"{s['title']}  ({count}轮)" if count else s["title"]

            widget = SessionItemWidget(sid, label)
            widget.clicked.connect(self._on_widget_clicked)
            widget.rename_requested.connect(self.session_rename_requested.emit)
            widget.delete_requested.connect(self.session_delete_requested.emit)

            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, sid)
            item.setSizeHint(widget.sizeHint())

            self._session_list.addItem(item)
            self._session_list.setItemWidget(item, widget)
            self._session_widgets[sid] = widget

            if s.get("is_current"):
                current_id = sid

        self._session_list.blockSignals(False)

        if current_id:
            self._select_by_id(current_id)

    def add_session_item(self, session_id: str, title: str) -> None:
        widget = SessionItemWidget(session_id, title)
        widget.clicked.connect(self._on_widget_clicked)
        widget.rename_requested.connect(self.session_rename_requested.emit)
        widget.delete_requested.connect(self.session_delete_requested.emit)

        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, session_id)
        item.setSizeHint(widget.sizeHint())

        self._session_list.insertItem(0, item)
        self._session_list.setItemWidget(item, widget)
        self._session_widgets[session_id] = widget

        self._select_by_id(session_id)

    def set_current_item(self, session_id: str) -> None:
        self._select_by_id(session_id)

    def update_session_title(self, session_id: str, title: str) -> None:
        widget = self._session_widgets.get(session_id)
        if widget is not None:
            widget.set_title(title)

    def remove_session_item(self, session_id: str) -> None:
        self._session_widgets.pop(session_id, None)
        for i in range(self._session_list.count()):
            item = self._session_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == session_id:
                self._session_list.takeItem(i)
                break

    # ── 内部 ──

    def _select_by_id(self, session_id: str) -> None:
        self._updating_selection = True
        for i in range(self._session_list.count()):
            item = self._session_list.item(i)
            if not item:
                continue
            sid = item.data(Qt.ItemDataRole.UserRole)
            widget = self._session_list.itemWidget(item)
            if isinstance(widget, SessionItemWidget):
                widget.set_selected(sid == session_id)
            if sid == session_id:
                self._session_list.setCurrentItem(item)
        self._updating_selection = False

    def _on_widget_clicked(self, session_id: str) -> None:
        self.session_selected.emit(session_id)

    def _on_current_changed(self, current: QListWidgetItem,
                            previous: QListWidgetItem | None) -> None:
        if self._updating_selection:
            return
        if current is None:
            return
        sid = current.data(Qt.ItemDataRole.UserRole)
        if not sid:
            return
