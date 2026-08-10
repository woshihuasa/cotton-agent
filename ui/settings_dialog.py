"""
设置对话框 — API Key 管理 GUI

包含 DeepSeek、Embedding (硅基流动)、高德地图、Tavily 四组 API 配置表单。
密码输入框右侧带"小眼睛"图标，可切换明文/密文显示。
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
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
QLabel#formLabel {
    font-size: 13px;
    color: #555555;
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

    密码输入框使用 PasswordLineEdit，带小眼睛切换。
    点保存后写回 .env 文件。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.setMinimumWidth(500)
        self.setMaximumWidth(600)
        self.setModal(True)
        self.setStyleSheet(DIALOG_STYLE)

        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(24, 24, 24, 24)

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
        root.addWidget(ds_group)

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
        root.addWidget(emb_group)

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
        root.addWidget(other_group)

        # ── 按钮 ──
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
