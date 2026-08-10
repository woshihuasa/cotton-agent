"""
棉花问答智能体 — PyQt6 现代 AI 对话风格界面 (v2 - 多会话侧边栏)
布局：左侧 SideBar + 右侧聊天区 (QScrollArea + InputFrame)
特性：隐藏式滚动条 · 对话气泡 · 思考过程可视化 · 流式打字机 · 多会话管理
"""

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QThread,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QPainterPath
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
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

import re
import sys
import logging
from pathlib import Path

import markdown

log = logging.getLogger("ui")

from config import AppConfig, resource_path
from core.config_manager import ConfigManager
from core.kb_builder import needs_rebuild, rebuild_knowledge_base, verify_embedding
from core.knowledge_base import KnowledgeBase
from core.rag_engine import RAGEngine
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
QLabel#bubbleLabel table {
    border-collapse: collapse;
    width: 100%;
}
QLabel#bubbleLabel th, QLabel#bubbleLabel td {
    border: 1px solid #DDDDDD;
    padding: 6px;
}
QLabel#bubbleLabel code {
    background-color: #F0F0F0;
    padding: 2px 4px;
    border-radius: 4px;
    font-family: Consolas;
}
QLabel#bubbleLabel pre {
    background-color: #F6F8FA;
    padding: 10px;
    border-radius: 6px;
    border: 1px solid #E0E0E0;
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
QPushButton#modelBtn {
    background-color: #F0F0F0;
    border: 1px solid #F0F0F0;
    border-radius: 12px;
    padding: 4px 10px;
    min-height: 20px;
    color: #333333;
    font-size: 13px;
}
QPushButton#modelBtn:hover {
    border: 1px solid #D0D0D0;
    background-color: #E8E8E8;
}
QPushButton#modelBtn:pressed {
    background-color: #D8D8D8;
}

QCheckBox#thinkCheck {
    spacing: 0px;
    background-color: #F0F0F0;
    border: 1px solid #F0F0F0;
    border-radius: 12px;
    padding: 4px 10px;
    min-height: 20px;
    color: #333333;
    font-size: 13px;
}
QCheckBox#thinkCheck::indicator {
    width: 0px;
    height: 0px;
    image: none;
}
QCheckBox#thinkCheck:checked {
    background-color: #E6F0FF;
    border: 1px solid #E6F0FF;
    color: #0066CC;
}
QCheckBox#thinkCheck:hover {
    border: 1px solid #D0D0D0;
}
QCheckBox#thinkCheck:checked:hover {
    border: 1px solid #B0D8FF;
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

# ── SVG 箭头图标绘制 ───────────────────────────────────────


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
            self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        else:
            self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
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
                # 将剩余的 Markdown 文本转为 HTML 重新渲染
                html = self._render_markdown(raw)
                self._label.setText(html)
            self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    @staticmethod
    def _render_markdown(text: str) -> str:
        """将 Markdown 文本转为 HTML。"""
        return markdown.markdown(text, extensions=['tables', 'fenced_code'])

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

    def __init__(
        self,
        engine: RAGEngine,
        question: str,
        model_name: str | None = None,
        enable_thinking: bool = False,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._question = question
        self._model_name = model_name
        self._enable_thinking = enable_thinking

    def run(self) -> None:
        try:
            for msg_type, chunk in self._engine.ask(
                self._question,
                model_name=self._model_name,
                enable_thinking=self._enable_thinking,
            ):
                self.new_text_signal.emit(msg_type, chunk)
        except Exception as e:
            self.new_text_signal.emit("content", f"❌ 处理失败：{e}")
            self.new_text_signal.emit("error", "")
        finally:
            self.finished_signal.emit()


# ── 主窗口 ─────────────────────────────────────────────────


class CottonAgentWindow(QMainWindow):
    """新疆棉花智能助手主窗口 (v2 - 多会话侧边栏)。

    布局：左 SideBar | 右 (聊天区 + 输入框)
    """

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

        self._init_ui()
        self._apply_styles()
        self._refresh_sidebar()
        self._load_history()

        # 延迟启动知识库自动检测（窗口先显示，避免启动阻塞）
        QTimer.singleShot(300, self._ensure_knowledge_base)
        # 延迟启动更新检查
        QTimer.singleShot(2000, self._check_for_update)

        # 更新下载完成后退出时拉起 updater.exe
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._on_about_to_quit)
        self._pending_update_zip: str | None = None

    # ── 知识库自动构建与维护 ────────────────────────────

    def _ensure_knowledge_base(self, force_embedding_check: bool = False) -> None:
        """启动后自动检测知识库状态：

        1. Embedding API 未配置/不可用 → 跳过（降级为数据工具模式）
        2. 指纹一致且库已构建 → 就绪
        3. 首次启动 / 文档变更 → 后台线程全量重建
        """
        ok, msg = verify_embedding(force=force_embedding_check)
        if not ok:
            print(f"[KB] 知识库未构建：{msg}")
            log.info("知识库未构建：%s", msg)
            return
        if not needs_rebuild():
            print("[KB] 知识库已就绪。")
            log.info("知识库已就绪。")
            return
        print("[KB] 检测到知识库需要构建，启动后台构建...")
        log.info("检测到知识库需要构建，启动后台构建...")
        self._kb_worker = KBWorker()
        self._kb_worker.status_signal.connect(self._on_kb_build_finished)
        self._kb_worker.start()

    def _on_kb_build_finished(self, ok: bool, msg: str) -> None:
        """构建线程完成回调（主线程）：刷新知识库引用或降级提示。"""
        if ok:
            try:
                self._engine.kb = KnowledgeBase()  # 刷新为已构建的库
            except Exception as e:
                print(f"[KB] 刷新知识库失败: {e}")
                log.warning("刷新知识库失败: %s", e)
            print(f"[KB] {msg}")
            log.info("知识库构建完成：%s", msg)
        else:
            print(f"[KB] {msg}")
            log.warning("知识库构建失败：%s", msg)
            QMessageBox.warning(
                self,
                "知识库构建失败",
                f"{msg}\n\n已降级为数据工具问答模式，产量/价格/图表等本地工具仍可用。"
                "修复配置后重启应用将自动重试。",
            )

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
            + TOGGLE_BTN_STYLE + CONTROL_STYLE
        )

    # ── UI 构建 ──────────────────────────────────────────

    def _init_ui(self) -> None:
        """构建整体布局：侧边栏 | 聊天区。"""
        self.setWindowTitle("棉花智能助手")
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

        self._model_btn = QPushButton("deepseek-v4-flash")
        self._model_btn.setObjectName("modelBtn")
        self._model_btn.setIcon(QIcon(str(resource_path("ui/icons/arrow_down.svg"))))
        self._model_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._model_btn.setStyleSheet(
            self._model_btn.styleSheet() + "text-align: left; padding-left: 12px;"
        )
        self._model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_btn.setToolTip("选择 AI 模型")
        self._model_btn.clicked.connect(self._show_model_menu)
        input_toolbar.addWidget(self._model_btn)
        input_toolbar.addSpacing(8)
        self._think_check = QCheckBox("深度思考")
        self._think_check.setObjectName("thinkCheck")
        input_toolbar.addWidget(self._think_check)

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
        idx = self._bubble_layout.count() - 1
        self._bubble_layout.insertWidget(idx, bubble)
        return bubble

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

    def _show_model_menu(self) -> None:
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
        """)
        for model in ["deepseek-v4-flash", "deepseek-v4-pro"]:
            action = menu.addAction(model)
            action.triggered.connect(
                lambda checked, m=model: self._on_model_selected(m)
            )
        pos = self._model_btn.mapToGlobal(QPoint(0, self._model_btn.height()))
        menu.exec(pos)

    def _on_model_selected(self, model: str) -> None:
        self._model_btn.setText(model)

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

        model_name = self._model_btn.text()
        enable_thinking = self._think_check.isChecked()

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
        )
        self._worker.new_text_signal.connect(self._on_update_text)
        self._worker.finished_signal.connect(self._on_stream_finished)
        self._worker.start()

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
        """关闭前停止定时器。"""
        self._update_timer.stop()
        super().closeEvent(event)
