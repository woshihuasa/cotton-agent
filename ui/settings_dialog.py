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
    QListWidget,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.config_manager import ConfigManager

# ── 样式 ───────────────────────────────────────────

DIALOG_STYLE = """
QDialog {
    background-color: #FFFFFF;
}
/* ── 设置卡片：**只留边框，填充透明** ──
   ★ 原来只写了 border，没写 background-color，Qt 就用平台默认调色板
     （实测 `QGroupBox` 的 Window 色是 #efefef），于是每张卡片都是一个
     **灰色矩形块**，和白色对话框格格不入。
     这里显式声明透明，让卡片"只剩一个圆角描边"。 */
QGroupBox {
    font-size: 14px;
    font-weight: 600;
    color: #333333;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 16px;
    background-color: transparent;
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
    background-color: #FFFFFF;
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
    background: transparent;
}
QLabel a {
    color: #0066CC;
    text-decoration: none;
}
/* ── 左侧导航（把各设置卡片收进菜单）── */
QListWidget#settingsNav {
    background-color: #FAFAFA;
    border: none;
    border-right: 1px solid #EEEEEE;
    outline: none;
    padding: 8px 6px;
    font-size: 13px;
}
QListWidget#settingsNav::item {
    color: #444444;
    padding: 9px 12px;
    border-radius: 6px;
    margin: 2px 0;
}
QListWidget#settingsNav::item:hover {
    background-color: #F0F1F3;
}
QListWidget#settingsNav::item:selected {
    background-color: #E9EBEF;
    color: #111111;
    font-weight: 600;
}
QPushButton {
    padding: 8px 20px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    background: transparent;
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
    background-color: transparent;
    color: #333333;
    border: 1px solid #D0D0D0;
}
QPushButton#cancelBtn:hover {
    background-color: #F5F5F5;
}
QPushButton#cancelBtn:disabled {
    background-color: transparent;
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

    def __init__(self, parent=None, *, scheduler=None, page: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        # ★ 比原来宽：左侧多了 132px 导航栏，宽度不加的话右侧表单会被压扁
        self.setMinimumWidth(660)
        self.setMaximumWidth(780)
        self.setModal(True)
        self.setStyleSheet(DIALOG_STYLE)

        #: 定时触发层的调度器（「推送」那一页要用）。None 表示还没启动，
        #: 那一页会自己显示原因，而不是让整个设置打不开。
        self._scheduler = scheduler
        #: 打开时定位到哪一页（抽屉里的齿轮用它直接跳到「推送」）
        self._want_page = page

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
        self.resize(720, height)

        if avail is not None:
            frame = self.frameGeometry()
            frame.moveCenter(avail.center())
            self.move(frame.topLeft())

    def _make_page(self, *widgets: QWidget) -> QScrollArea:
        """把若干卡片包成一个**可滚动的设置页**。

        ★ 每个分类各自滚动：原来只有一个全局滚动区，切了分类还是同一根滚动条，
          会让人以为"还在同一页"。分开之后每个分类的滚动位置互不干扰。
        """
        page = QWidget()
        page.setObjectName("scrollContent")
        col = QVBoxLayout(page)
        col.setSpacing(16)
        col.setContentsMargins(0, 0, 8, 0)          # 右侧留出滚动条位置
        for w in widgets:
            col.addWidget(w)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _select_page(self, title: str) -> None:
        """按导航项**文字**定位页 —— 用文字而不是索引，加页时不会错位。"""
        text = str(title or "").strip()
        if not text:
            return
        for i in range(self._nav.count()):
            if self._nav.item(i).text() == text:
                self._nav.setCurrentRow(i)
                return

    def _on_page_changed(self, row: int) -> None:
        """切到「推送」时刷新面板。

        ★ 必须刷：面板上的「下次运行」是时间相关的，设置窗口可能开很久；
          而且用户可能刚在别处手动跑过一次。
        """
        panel = getattr(self, "_schedule_panel", None)
        if panel is None:
            return
        item = self._nav.item(row)
        if item is not None and item.text() == "推送":
            panel.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(0, 0, 0, 0)      # 导航要贴边，外边距交给各块自己

        # ── 主体：左侧导航 | 右侧设置页 ──
        # ★ 2026-10-18：把原来"一整列往下排"的卡片收进左侧菜单，
        #   点菜单项只显示对应的一组，右侧不再需要一直滚动找设置项。
        body = QHBoxLayout()
        body.setSpacing(0)
        body.setContentsMargins(0, 0, 0, 0)

        self._nav = QListWidget()
        self._nav.setObjectName("settingsNav")
        self._nav.setFixedWidth(132)
        self._nav.setFrameShape(QFrame.Shape.NoFrame)
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._stack = QStackedWidget()
        self._stack.setObjectName("settingsStack")

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

        # ── 模型（多提供商管理，含「测试连接」与「设为默认」）──
        # ⚠️ 该分组**即时保存**（增删改立刻落盘），不依赖底部的"保存"按钮 ——
        #    因为密钥写入是独立于 .env 的凭据层，混进 _on_save 会让"取消"语义变复杂。
        prov_group = QGroupBox("模型")
        prov_layout = QVBoxLayout(prov_group)
        prov_layout.setSpacing(8)
        try:
            from ui.provider_editor import ProviderGroup
            self._providers = ProviderGroup()
            prov_layout.addWidget(self._providers)
        except Exception as e:                          # noqa: BLE001
            # 界面降级：提供商页初始化失败**不应让整个设置打不开**
            err = QLabel(f"模型提供商管理暂不可用：{type(e).__name__}: {e}")
            err.setWordWrap(True)
            err.setObjectName("formLabel")
            prov_layout.addWidget(err)
            self._providers = None

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

        # ── 装页：顺序即菜单顺序 ──
        #
        # ★ 「推送」放在这里而不是做成独立窗口：配置在用户的习惯里属于"设置"。
        #   抽屉里的齿轮是快捷键 —— 打开同一个窗口并直接定位到这一页。
        try:
            from ui.schedule_panel import SchedulePanel
            self._schedule_panel = SchedulePanel(self._scheduler)
        except Exception as e:                          # noqa: BLE001
            self._schedule_panel = None
            err = QLabel(f"推送设置暂不可用：{type(e).__name__}: {e}")
            err.setWordWrap(True)
            err.setObjectName("formLabel")

        pages = [
            ("模型", self._make_page(prov_group)),
            ("Embedding", self._make_page(emb_group)),
            ("其他 API", self._make_page(other_group)),
            ("Web 服务", self._make_page(web_group)),
        ]
        if self._schedule_panel is not None:
            pages.append(("推送", self._make_page(self._schedule_panel)))
        else:
            pages.append(("推送", self._make_page(err)))

        for title, page in pages:
            self._nav.addItem(title)
            self._stack.addWidget(page)

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)          # 默认停在「模型」——最常改的一项
        self._select_page(self._want_page)

        # 每次切到「推送」都重新读一次状态：可能刚手动跑过一次，或上一次触发
        # 已经过去，界面上的"下次运行"不该停在打开设置那一刻。
        self._nav.currentRowChanged.connect(self._on_page_changed)

        # 右侧页面外留一点边距（左导航贴边，右内容不贴边）
        right_wrap = QWidget()
        right_lay = QVBoxLayout(right_wrap)
        right_lay.setContentsMargins(18, 18, 18, 8)
        right_lay.setSpacing(0)
        right_lay.addWidget(self._stack)

        body.addWidget(self._nav)
        body.addWidget(right_wrap, 1)
        root.addLayout(body, 1)

        # ── 按钮（固定在底部，不随内容滚动）──
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(18, 0, 18, 16)
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
        # ⚠️ 这里**不再**加载 DEEPSEEK_* —— 那三个键仍留在 .env 里（作为
        # `LLM_PROVIDER` 为空时的向后兼容回退），但界面上已经不提供编辑入口：
        # 提供商与密钥统一在「模型」分组里管理（登记表 + 凭据层）。
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
        # ⚠️ **不再写入 DEEPSEEK_\*** —— 模型接入已交给「模型」分组
        # （登记表 + 凭据层 + `LLM_PROVIDER`）。这里若继续写那三个键，
        # 会出现"界面上没有入口、却在每次保存时被覆盖"的隐性副作用。
        ConfigManager.save({
            "EMBEDDING_API_KEY": self._emb_key.text().strip(),
            "EMBEDDING_BASE_URL": self._emb_url.text().strip() or "https://api.siliconflow.cn/v1",
            "EMBEDDING_MODEL": self._emb_model.text().strip() or "BAAI/bge-large-zh-v1.5",
            "AMAP_API_KEY": self._amap_key.text().strip(),
            "TAVILY_API_KEY": self._tavily_key.text().strip(),
        })
        self.accept()
