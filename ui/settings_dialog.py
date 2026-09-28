"""
设置对话框 — API Key 管理 GUI

包含 DeepSeek、Embedding (硅基流动)、高德地图、Tavily 四组 API 配置表单，
以及「Web 服务」的启动 / 停止控制。
密码输入框右侧带"小眼睛"图标，可切换明文/密文显示。

设置项较多时内容区放进 QScrollArea 滚动，窗口高度固定为屏幕可用高度的 82%
（上限 680px），避免对话框被内容撑到超出屏幕。
"""

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import ConfigManager

# ── 样式 ───────────────────────────────────────────

DIALOG_STYLE = """
QDialog {
    background-color: #FFFFFF;
}
QGroupBox {
    font-size: 14px;
    font-weight: 600;
    color: #333333;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 16px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 16px;
    padding: 0 6px;
}
QLineEdit {
    border: 1px solid #D0D0D0;
    border-radius: 6px;
    padding: 8px 32px 8px 10px;
    font-size: 13px;
    color: #333333;
}
QLineEdit:focus {
    border-color: #4A90D9;
}
QLineEdit:disabled {
    background-color: #F5F5F5;
    color: #999999;
}
QLabel#formLabel {
    font-size: 13px;
    color: #555555;
}
QLabel a {
    color: #0066CC;
    text-decoration: none;
}
QPushButton {
    padding: 8px 20px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
}
QPushButton#saveBtn {
    background-color: #000000;
    color: #FFFFFF;
    border: none;
}
QPushButton#saveBtn:hover {
    background-color: #333333;
}
QPushButton#cancelBtn {
    background-color: #F5F5F5;
    color: #333333;
    border: 1px solid #D0D0D0;
}
QPushButton#cancelBtn:hover {
    background-color: #E8E8E8;
}
QPushButton#cancelBtn:disabled {
    background-color: #FAFAFA;
    color: #BBBBBB;
    border-color: #E8E8E8;
}
/* ── 滚动区：窗口高度不足时内容可滚动，而不是撑大对话框 ── */
QScrollArea {
    border: none;
    background: transparent;
}
QWidget#scrollContent {
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #C8C8C8;
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: #A8A8A8;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
"""

# ── 图标绘制（懒加载，首次使用在 QApplication 之后才执行）──

_eye_icon_cache: QIcon | None = None
_eye_off_icon_cache: QIcon | None = None


def _make_eye_icon() -> QIcon:
    """绘制睁眼图标 (16x16), 灰色 #888888。"""
    size = 16
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(
        QColor("#888888"), 1.5,
        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
    )
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # 眼眶椭圆
    painter.drawEllipse(1, 2, 14, 12)

    # 瞳孔 (实心圆)
    painter.setBrush(QColor("#888888"))
    painter.drawEllipse(5, 5, 6, 6)

    painter.end()
    return QIcon(pixmap)


def _make_eye_off_icon() -> QIcon:
    """绘制闭眼 / 遮挡图标 (16x16), 灰色 #888888, 带斜线。"""
    size = 16
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(
        QColor("#888888"), 1.5,
        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
    )
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # 眼眶椭圆
    painter.drawEllipse(1, 2, 14, 12)

    # 瞳孔 (实心圆)
    painter.setBrush(QColor("#888888"))
    painter.drawEllipse(5, 5, 6, 6)

    # 遮挡斜线
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#888888"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(2, 1, 15, 15)

    painter.end()
    return QIcon(pixmap)


def _get_eye_icon() -> QIcon:
    """懒加载睁眼图标（首次调用时才创建 QPixmap/QPainter）。"""
    global _eye_icon_cache
    if _eye_icon_cache is None:
        _eye_icon_cache = _make_eye_icon()
    return _eye_icon_cache


def _get_eye_off_icon() -> QIcon:
    """懒加载闭眼图标。"""
    global _eye_off_icon_cache
    if _eye_off_icon_cache is None:
        _eye_off_icon_cache = _make_eye_off_icon()
    return _eye_off_icon_cache


# ── 密码输入框控件 ─────────────────────────────────

class PasswordLineEdit(QLineEdit):
    """带密码明文/密文切换（小眼睛）的输入框。

    默认密文模式，右侧显示睁眼图标。
    点击图标在明文 / 密文之间切换。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)

        self._action = QAction(_get_eye_icon(), "显示/隐藏", self)
        self._action.triggered.connect(self._toggle_visibility)
        self.addAction(self._action, QLineEdit.ActionPosition.TrailingPosition)

    def _toggle_visibility(self) -> None:
        """在密文 ↔ 明文之间切换。"""
        if self.echoMode() == QLineEdit.EchoMode.Password:
            self.setEchoMode(QLineEdit.EchoMode.Normal)
            self._action.setIcon(_get_eye_off_icon())
        else:
            self.setEchoMode(QLineEdit.EchoMode.Password)
            self._action.setIcon(_get_eye_icon())


# ── 设置对话框 ─────────────────────────────────────

class SettingsDialog(QDialog):
    """API Key 配置对话框。

    - DeepSeek: API Key / Base URL / Model
    - Embedding (硅基流动): API Key / Base URL / Model
    - 高德地图: API Key
    - Tavily 搜索: API Key
    - Web 服务: 端口 + 启动 / 停止 + 在浏览器中打开

    密码输入框使用 PasswordLineEdit，带小眼睛切换。
    点保存后写回 .env 文件。

    内容区可滚动，窗口高度不会超出屏幕。
    """

    # 窗口高度上限（再高也没必要，笔记本屏幕多半放不下）
    MAX_HEIGHT = 680
    MIN_HEIGHT = 420

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.setMinimumWidth(500)
        self.setMaximumWidth(600)
        self.setModal(True)
        self.setStyleSheet(DIALOG_STYLE)

        self._fit_to_screen()
        self._build_ui()
        self._load_values()
        self._sync_web_state()      # 服务可能仍在后台运行，界面状态需与实际一致

    def _fit_to_screen(self) -> None:
        """按屏幕可用高度确定窗口尺寸并居中。

        设置项累积后内容总高会超过屏幕（笔记本上尤其明显），窗口被窗口管理器
        反复钳制位置、Qt 随后重算布局，表现为拖动时闪烁、松手回弹。这里把高度
        钉在屏幕可用高度的 82% 以内，超出部分交给内部滚动区滚动。
        """
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None

        height = self.MAX_HEIGHT
        if avail is not None:
            height = max(self.MIN_HEIGHT, min(self.MAX_HEIGHT, int(avail.height() * 0.82)))
        self.resize(560, height)

        if avail is not None:
            frame = self.frameGeometry()
            frame.moveCenter(avail.center())
            self.move(frame.topLeft())

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 16)

        # ── 可滚动内容区 ──
        # 所有设置分组放进 content，由 QScrollArea 承载：
        # 窗口放不下时内容滚动，而不是把对话框撑到屏幕之外。
        content = QWidget()
        content.setObjectName("scrollContent")
        col = QVBoxLayout(content)
        col.setSpacing(16)
        col.setContentsMargins(0, 0, 8, 0)      # 右侧留出滚动条位置

        # ── DeepSeek ──
        ds_group = QGroupBox("DeepSeek API")
        ds_form = QFormLayout(ds_group)
        ds_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._ds_key = PasswordLineEdit()
        self._ds_key.setPlaceholderText("sk-...")
        self._ds_key.setMinimumWidth(300)
        ds_form.addRow(self._label("API Key:"), self._ds_key)

        self._ds_url = QLineEdit()
        self._ds_url.setPlaceholderText("https://api.deepseek.com")
        ds_form.addRow(self._label("Base URL:"), self._ds_url)

        self._ds_model = QLineEdit()
        self._ds_model.setPlaceholderText("deepseek-v4-flash")
        ds_form.addRow(self._label("Model:"), self._ds_model)
        col.addWidget(ds_group)

        # ── Embedding ──
        emb_group = QGroupBox("Embedding API (硅基流动)")
        emb_form = QFormLayout(emb_group)
        emb_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._emb_key = PasswordLineEdit()
        self._emb_key.setPlaceholderText("sk-...")
        emb_form.addRow(self._label("API Key:"), self._emb_key)

        self._emb_url = QLineEdit()
        self._emb_url.setPlaceholderText("https://api.siliconflow.cn/v1")
        emb_form.addRow(self._label("Base URL:"), self._emb_url)

        self._emb_model = QLineEdit()
        self._emb_model.setPlaceholderText("BAAI/bge-large-zh-v1.5")
        emb_form.addRow(self._label("Model:"), self._emb_model)
        col.addWidget(emb_group)

        # ── 其他 API ──
        other_group = QGroupBox("其他 API")
        other_form = QFormLayout(other_group)
        other_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._amap_key = PasswordLineEdit()
        self._amap_key.setPlaceholderText("高德地图 Web API Key")
        other_form.addRow(self._label("高德地图 Key:"), self._amap_key)

        self._tavily_key = PasswordLineEdit()
        self._tavily_key.setPlaceholderText("tvly-...")
        other_form.addRow(self._label("Tavily Search Key:"), self._tavily_key)
        col.addWidget(other_group)

        # ── Web 服务 ──
        web_group = QGroupBox("Web 服务（浏览器访问）")
        web_layout = QVBoxLayout(web_group)
        web_layout.setSpacing(8)

        hint = QLabel("启动后可用浏览器打开（手机连同一 WiFi 也能访问）。\n"
                      "LLM Key 由用户在网页上填写（存浏览器本地）；知识库检索由本机配置提供。")
        hint.setObjectName("formLabel")
        hint.setWordWrap(True)
        web_layout.addWidget(hint)

        web_row = QHBoxLayout()
        web_row.addWidget(self._label("端口:"))
        self._web_port = QLineEdit("8000")
        self._web_port.setFixedWidth(90)
        web_row.addWidget(self._web_port)

        self._web_btn = QPushButton("启动服务")
        self._web_btn.setObjectName("cancelBtn")
        self._web_btn.clicked.connect(self._toggle_web)
        web_row.addWidget(self._web_btn)

        self._web_open_btn = QPushButton("打开浏览器")
        self._web_open_btn.setObjectName("cancelBtn")
        self._web_open_btn.setToolTip("用系统默认浏览器打开服务页面")
        self._web_open_btn.clicked.connect(self._open_in_browser)
        self._web_open_btn.setEnabled(False)        # 服务未启动时禁用
        web_row.addWidget(self._web_open_btn)

        web_row.addStretch()
        web_layout.addLayout(web_row)

        # 状态文字：运行中时把地址渲染为可点击链接（点击即用默认浏览器打开）
        self._web_status = QLabel("未启动")
        self._web_status.setObjectName("formLabel")
        self._web_status.setWordWrap(True)
        self._web_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse        # 仍可选中复制
            | Qt.TextInteractionFlag.LinksAccessibleByMouse)    # 也可点击链接
        self._web_status.setOpenExternalLinks(True)
        web_layout.addWidget(self._web_status)

        col.addWidget(web_group)
        col.addStretch(1)

        # ── 滚动容器 ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # ── 按钮（固定在底部，不随内容滚动）──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("cancelBtn")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("saveBtn")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        root.addLayout(btn_layout)

    @staticmethod
    def _label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("formLabel")
        lbl.setFixedWidth(100)
        return lbl

    def _load_values(self) -> None:
        data = ConfigManager.load()
        self._ds_key.setText(data.get("DEEPSEEK_API_KEY", ""))
        self._ds_url.setText(data.get("DEEPSEEK_BASE_URL", ""))
        self._ds_model.setText(data.get("DEEPSEEK_MODEL", ""))
        self._emb_key.setText(data.get("EMBEDDING_API_KEY", ""))
        self._emb_url.setText(data.get("EMBEDDING_BASE_URL", ""))
        self._emb_model.setText(data.get("EMBEDDING_MODEL", ""))
        self._amap_key.setText(data.get("AMAP_API_KEY", ""))
        self._tavily_key.setText(data.get("TAVILY_API_KEY", ""))

    # ── Web 服务 ──────────────────────────────────

    def _web_port_value(self) -> int | None:
        """读取端口输入框；非法时返回 None。"""
        try:
            return int(self._web_port.text().strip() or "8000")
        except ValueError:
            return None

    @staticmethod
    def _running_html(local: str, lan: str) -> str:
        """运行中的状态文字：两个地址都可点击，直接用默认浏览器打开。"""
        return (f'已启动，点击访问：<a href="{local}">{local}</a><br>'
                f'局域网（手机同 WiFi 可访问）：<a href="{lan}">{lan}</a>')

    def _sync_web_state(self) -> None:
        """把界面状态对齐到服务的真实运行状态。

        服务跑在后台线程、生命周期独立于本对话框（关掉设置也不会停止），
        所以每次打开设置都重新同步一次，避免界面显示与实际不符。
        """
        from core import web_server

        if web_server.is_running():
            port = self._web_port_value() or 8000
            local, lan = web_server.access_urls(port)
            self._web_btn.setText("停止服务")
            self._web_port.setEnabled(False)        # 防止改端口后地址对不上
            self._web_open_btn.setEnabled(True)
            self._web_status.setText(self._running_html(local, lan))
        else:
            self._web_btn.setText("启动服务")
            self._web_port.setEnabled(True)
            self._web_open_btn.setEnabled(False)
            self._web_status.setText("未启动")

    def _open_in_browser(self) -> None:
        """用系统默认浏览器打开服务页面（QDesktopServices 跨平台，打包后同样可用）。"""
        from core import web_server

        if not web_server.is_running():
            self._sync_web_state()
            return
        local, _ = web_server.access_urls(self._web_port_value() or 8000)
        QDesktopServices.openUrl(QUrl(local))

    def _toggle_web(self) -> None:
        """启动 / 停止 Web 服务。

        服务运行在**后台线程**中，独立于本对话框 —— 关闭窗口不会停止服务。
        """
        from core import web_server

        if web_server.is_running():
            _, msg = web_server.stop()
            self._web_btn.setText("启动服务")
            self._web_port.setEnabled(True)
            self._web_open_btn.setEnabled(False)
            self._web_status.setText(msg)
            return

        port = self._web_port_value()
        if port is None:
            self._web_status.setText("端口必须是数字")
            return

        ok, msg = web_server.start(port=port)
        if not ok:
            self._web_status.setText(msg)
            return

        local, lan = web_server.access_urls(port)
        self._web_btn.setText("停止服务")
        self._web_port.setEnabled(False)
        self._web_open_btn.setEnabled(True)
        self._web_status.setText(self._running_html(local, lan))

    def _on_save(self) -> None:
        ConfigManager.save({
            "DEEPSEEK_API_KEY": self._ds_key.text().strip(),
            "DEEPSEEK_BASE_URL": self._ds_url.text().strip() or "https://api.deepseek.com",
            "DEEPSEEK_MODEL": self._ds_model.text().strip() or "deepseek-v4-flash",
            "EMBEDDING_API_KEY": self._emb_key.text().strip(),
            "EMBEDDING_BASE_URL": self._emb_url.text().strip() or "https://api.siliconflow.cn/v1",
            "EMBEDDING_MODEL": self._emb_model.text().strip() or "BAAI/bge-large-zh-v1.5",
            "AMAP_API_KEY": self._amap_key.text().strip(),
            "TAVILY_API_KEY": self._tavily_key.text().strip(),
        })
        self.accept()
