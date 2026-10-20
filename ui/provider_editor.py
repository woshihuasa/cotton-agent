# -*- coding: utf-8 -*-
"""模型提供商管理界面 —— 卡片式列表 + 编辑对话框。

【设计参照】DeepSeek Harness（DSH）的 `dsh-client-ui-settings-models`，以及用户提供的界面截图：
        **圆角卡片行 · 名称 + 状态点 + [编辑]/[删除] · 虚线「＋ 添加模型提供商」**。
        不用表格、不用表头 —— 表格在这里显得生硬，卡片更像"一列设置项"。

        照搬的四条原则：
          · **密钥只写、绝不回显** —— 输入框初始为空，保存后只显示掩码指纹（`sk-b…c42b`）；
          · **状态点"只有确认才显示"** —— 绿=已配置、红=未配置，不确定就不显示；
          · **内置项的端点只读** —— 端点里编着地域与计费类型，改错会得到"看着像密钥失效"的 401；
          · **测试连接区分四类失败** —— 因为"端点/空间不匹配"也报 401，错误信息会骗人。

        另加一条本项目必需的能力（截图里没有、但去掉旧配置栏后必须有）：
          · **「设为默认」** —— 决定 `LLM_PROVIDER` 指向谁。没有它，用户就只能手工改 `.env`。

【安全红线】本模块**绝不调用**取完整密钥的那个内部函数 ——
        `tests/check_credentials.py` 与 `tests/check_providers_ui.py` 有 AST 级断言盯着。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core import credentials, providers

#: 状态点：绿=已配置，红=未配置（与截图一致，只用两个颜色，不引入"不确定"的第三态）
DOT_ON, DOT_OFF = "●", "●"
COLOR_ON, COLOR_OFF = "#2e9e5b", "#d4380d"

_CARD_QSS = """
QFrame#providerCard {
    background: #ffffff;
    border: 1px solid #e3e5e8;
    border-radius: 10px;
}
QFrame#providerCard:hover { border-color: #c9ced6; }
QLabel#providerName { font-size: 14px; color: #1f2328; }
QLabel#defaultBadge {
    font-size: 11px; color: #2e9e5b;
    border: 1px solid #b7e2c8; border-radius: 6px;
    padding: 0px 6px; background: #f2fbf6;
}
QPushButton#cardBtn {
    border: 1px solid #e3e5e8; border-radius: 8px;
    padding: 4px 14px; background: #ffffff; color: #1f2328;
}
QPushButton#cardBtn:hover { background: #f5f6f8; }
QPushButton#dangerBtn {
    border: none; padding: 4px 10px;
    background: transparent; color: #d4380d;
}
QPushButton#dangerBtn:hover { color: #a82608; }
QPushButton#addProvider {
    border: 1px dashed #cfd4dc; border-radius: 10px;
    padding: 12px; background: #ffffff; color: #57606a; font-size: 13px;
}
QPushButton#addProvider:hover { background: #f7f8fa; border-color: #b9c0ca; }
"""


def _status(name: str) -> tuple[str, str]:
    """返回 (状态点颜色, 提示文字)。★ 只用 `has_key`/`fingerprint`，**不取完整密钥**。"""
    if not credentials.has_key(name):
        return COLOR_OFF, "未配置密钥"
    fp = credentials.fingerprint(name)
    src = credentials.source(name)
    where = "文件" if src == "file" else ("环境变量" if src == "env" else "")
    return COLOR_ON, f"{fp}" + (f"（{where}）" if where else "")


def default_provider_name() -> str:
    """当前默认服务商名（读 `LLM_PROVIDER`；空串表示尚未指定）。"""
    try:
        from config import AppConfig
        n = (getattr(AppConfig, "LLM_PROVIDER", "") or "").strip()
    except Exception:                                        # noqa: BLE001
        n = ""
    return n if n in providers.all_specs() else ""


class ProviderEditDialog(QDialog):
    """新增/编辑一个模型提供商。

    · **内置**项：`id` 与 `接口地址` **只读**（端点里编着地域与计费类型，
      改错会返回 401 且错误信息**看着像密钥失效**）；
    · **自定义**项：字段全可编辑，但端点**必须是 https**。
    """

    def __init__(self, spec: dict, *, is_new: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.spec = dict(spec)
        self.is_new = is_new
        self.builtin = bool(spec.get("builtin"))
        self.setWindowTitle(("添加模型提供商" if is_new else "编辑") + f" · {spec.get('label', '')}")
        self.setMinimumWidth(540)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 内部 id 创建后锁定：它是配置与日志的键
        self._id = QLineEdit(spec.get("name", ""))
        self._id.setPlaceholderText("仅小写字母/数字/_/-，例如 my-gateway")
        self._id.setEnabled(is_new)
        if not is_new:
            self._id.setToolTip("内部 id 创建后不可修改（它是配置与日志的键）")
        form.addRow(QLabel("内部 id:"), self._id)

        self._label = QLineEdit(spec.get("label", ""))
        self._label.setPlaceholderText("显示名称，例如 千问 Token Plan")
        form.addRow(QLabel("显示名称:"), self._label)

        self._url = QLineEdit(spec.get("base_url", ""))
        self._url.setPlaceholderText("https://gateway.example/v1")
        if self.builtin:
            self._url.setEnabled(False)
            self._url.setToolTip(
                "内置提供商的接口地址不可改 —— 地址里编着地域与计费类型，"
                "写错会返回 401 且错误信息看着像「密钥失效」。")
        url_row = QHBoxLayout()
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.addWidget(self._url, 1)
        self._btn_test = QPushButton("测试连接")
        self._btn_test.setObjectName("cardBtn")
        self._btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_test.clicked.connect(self._on_test)
        # 新建时还没落库，测不了 —— 只有已存在的提供商才能测
        self._btn_test.setEnabled(not is_new)
        url_row.addWidget(self._btn_test)
        form.addRow(QLabel("接口地址:"), url_row)

        self._model = QLineEdit(spec.get("default_model", ""))
        self._model.setPlaceholderText("qwen3.7-plus")
        form.addRow(QLabel("默认模型:"), self._model)

        # API 密钥：只写。留空 = 不修改
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("留空 = 不修改现有密钥")
        form.addRow(QLabel("API 密钥:"), self._key)

        color, text = _status(spec.get("name", ""))
        self._status = QLabel(f'<span style="color:{color}">{DOT_OFF}</span> 当前：{text or "未配置"}')
        form.addRow(QLabel(""), self._status)

        # ★ 「设为默认」放在编辑页里 —— 卡片上保持截图那种只有 [编辑]/[删除] 的干净样子
        self._make_default = QCheckBox("设为默认模型提供商")
        self._make_default.setChecked(default_provider_name() == spec.get("name"))
        if self.builtin is False and spec.get("name") and not spec.get("name"):
            self._make_default.setEnabled(False)
        form.addRow(QLabel(""), self._make_default)

        root.addLayout(form)

        self._test_msg = QLabel("")
        self._test_msg.setWordWrap(True)
        self._test_msg.setStyleSheet("font-size:11px;")
        root.addWidget(self._test_msg)

        if spec.get("compat"):
            tip = QLabel("兼容性备注：" + "；".join(spec["compat"]))
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#8a919b; font-size:11px;")
            root.addWidget(tip)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def values(self) -> dict:
        return {
            "name": self._id.text().strip(),
            "label": self._label.text().strip(),
            "base_url": self._url.text().strip(),
            "default_model": self._model.text().strip(),
            "api_key": self._key.text(),          # 空串 = 不修改
            "make_default": self._make_default.isChecked(),
        }

    def _on_test(self) -> None:
        """测试连接 —— 调 `GET /models`（**不消耗生成 token**），并区分四类失败。"""
        name = self.spec.get("name", "")
        if not name:
            return
        self._btn_test.setEnabled(False)          # 进行中锁定，防重复点击
        self._test_msg.setText("正在测试连接…")
        self._test_msg.setStyleSheet("font-size:11px; color:#8a919b;")
        try:
            ok, detail = providers.test_connection(name)
        finally:
            self._btn_test.setEnabled(True)
        self._test_msg.setText(("✓ " if ok else "✗ ") + detail)
        self._test_msg.setStyleSheet("font-size:11px; color:%s;"
                                     % (COLOR_ON if ok else COLOR_OFF))

    def _on_ok(self) -> None:
        v = self.values()
        if not v["name"]:
            QMessageBox.warning(self, "缺少信息", "内部 id 不能为空。")
            return
        if not self.builtin and not v["base_url"].startswith("https://"):
            QMessageBox.warning(self, "接口地址不合规",
                                "接口地址必须以 https:// 开头。\n（明文 http 会以明文传输密钥）")
            return
        if not v["default_model"]:
            QMessageBox.warning(self, "缺少信息", "默认模型不能为空。")
            return
        self.accept()


class ProviderCard(QFrame):
    """一行提供商：名称 + 状态点 + [编辑] (+ [删除])。"""

    def __init__(self, spec: dict, *, is_default: bool, parent_group: "ProviderGroup") -> None:
        super().__init__(parent_group)
        self.spec = spec
        self.name = spec["name"]
        self.group = parent_group
        self.setObjectName("providerCard")
        self.setStyleSheet(_CARD_QSS)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 7, 12, 7)
        row.setSpacing(8)

        name = QLabel(spec.get("label") or self.name)
        name.setObjectName("providerName")
        row.addWidget(name)

        if is_default:
            badge = QLabel("默认")
            badge.setObjectName("defaultBadge")
            row.addWidget(badge)

        color, tip = _status(self.name)
        dot = QLabel(DOT_OFF)
        dot.setStyleSheet(f"color:{color}; font-size:15px;")
        dot.setToolTip(tip or "未配置密钥")
        row.addWidget(dot)
        row.addStretch(1)

        btn_edit = QPushButton("编辑")
        btn_edit.setObjectName("cardBtn")
        btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_edit.clicked.connect(lambda: self.group.edit_provider(self.name))
        row.addWidget(btn_edit)

        # 内置项不可删除；自定义项才有「删除」
        if not spec.get("builtin"):
            btn_del = QPushButton("删除")
            btn_del.setObjectName("dangerBtn")
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.clicked.connect(lambda: self.group.delete_provider(self.name))
            row.addWidget(btn_del)


class ProviderGroup(QWidget):
    """「模型」分组：卡片列表 + 添加按钮 + 状态提示。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        hint = QLabel("填入各提供商的 API 密钥即可使用其模型。密钥只写不读，保存后只显示掩码。")
        hint.setObjectName("formLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # 卡片列表放在滚动区里：提供商多了也不会把对话框撑爆
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setMaximumHeight(320)
        # ★ 2026-10-18：QScrollArea 的 viewport 默认用平台调色板（Windows 上是 #efefef），
        #   于是卡片背后会出现**一条灰色横带**（用户反馈的"灰色填充区"之一）。
        #   这里把滚动区与内容页都设为透明，让卡片直接浮在白色对话框上。
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #C8C8C8; border-radius: 4px;"
            " min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            " { background: transparent; }"
        )
        self._body = QWidget()
        self._list = QVBoxLayout(self._body)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll)

        self.btn_add = QPushButton("＋  添加模型提供商")
        self.btn_add.setObjectName("addProvider")
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.clicked.connect(self._on_add)
        root.addWidget(self.btn_add)

        self._msg = QLabel("")
        self._msg.setWordWrap(True)
        self._msg.setStyleSheet("font-size:11px;")
        root.addWidget(self._msg)

        self.refresh()

    # ---- 列表 ----
    def refresh(self) -> None:
        while self._list.count():
            it = self._list.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        specs = providers.all_specs()
        cur = default_provider_name()
        for name, spec in specs.items():
            self._list.addWidget(ProviderCard(spec, is_default=(name == cur),
                                              parent_group=self))
        self._list.addStretch(1)
        # ★ 让滚动区**贴合卡片数量**，而不是固定 320 高 ——
        # 固定高度会让 2 个提供商时下方留一大片空白（离屏预览时最明显）。
        row_h, pad = 46, 6
        h = min(len(specs) * row_h + pad, 320)
        self._scroll.setFixedHeight(max(h, row_h))

    def _say(self, text: str, *, error: bool = False) -> None:
        self._msg.setText(text)
        self._msg.setStyleSheet("font-size:11px; color:%s;"
                                % ("#d4380d" if error else "#8a919b"))

    # ---- 动作（供卡片回调）----
    def edit_provider(self, name: str) -> None:
        spec = providers.all_specs().get(name)
        if spec is None:
            return
        dlg = ProviderEditDialog(spec, is_new=False, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if v["api_key"]:
            credentials.set_key(name, v["api_key"])
        if not spec.get("builtin"):
            try:
                providers.upsert_custom(name, label=v["label"],
                                        base_url=v["base_url"],
                                        default_model=v["default_model"])
            except ValueError as e:
                QMessageBox.warning(self, "保存失败", str(e))
                return
        if v["make_default"] and name != default_provider_name():
            self.set_default(name)
            return                       # set_default 内部已 refresh 并提示
        self._say(f"已保存 {name}" + ("（密钥未改动）" if not v["api_key"] else ""))
        self.refresh()

    def set_default(self, name: str) -> None:
        """把某个提供商设为默认 —— 写 `LLM_PROVIDER` 到 `.env` 并热更新。

        ★ **这是去掉旧「DeepSeek API」栏后必须有的入口**：
        没有它，`LLM_PROVIDER` 就只能手工改 `.env`，而界面上已经改不到那三个键了。
        """
        try:
            from core.config_manager import ConfigManager
            from config import reload_config
            # 合并写入：保留注释与其它行不变
            ConfigManager.save({"LLM_PROVIDER": name, "LLM_MODEL": ""})
            reload_config()
        except Exception as e:                               # noqa: BLE001
            self._say(f"设为默认失败：{type(e).__name__}: {e}", error=True)
            self.refresh()
            return
        self._say(f"已把「{name}」设为默认模型提供商")
        self.refresh()

    def delete_provider(self, name: str) -> None:
        ok = QMessageBox.question(
            self, "删除模型提供商",
            f"确定删除「{name}」吗？它的密钥也会一并清除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            providers.delete_custom(name)
        except ValueError as e:
            QMessageBox.warning(self, "删除失败", str(e))
            return
        self._say(f"已删除 {name}")
        self.refresh()

    def set_default(self, name: str) -> None:
        """把某个提供商设为默认 —— 写 `LLM_PROVIDER` 到 `.env` 并热更新。"""
        try:
            from core.config_manager import ConfigManager
            from config import reload_config
        except Exception as e:                               # noqa: BLE001
            self._say(f"无法写入配置：{type(e).__name__}: {e}", error=True)
            return
        try:
            ConfigManager.save({"LLM_PROVIDER": name, "LLM_MODEL": ""})
            reload_config()
        except Exception as e:                               # noqa: BLE001
            self._say(f"设为默认失败：{type(e).__name__}: {e}", error=True)
            return
        self._say(f"已把「{name}」设为默认模型提供商")
        self.refresh()

    def _on_add(self) -> None:
        spec = {"name": "", "label": "", "base_url": "", "default_model": "", "builtin": False}
        dlg = ProviderEditDialog(spec, is_new=True, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        try:
            providers.upsert_custom(v["name"], label=v["label"],
                                    base_url=v["base_url"],
                                    default_model=v["default_model"])
        except ValueError as e:
            QMessageBox.warning(self, "添加失败", str(e))
            return
        if v["api_key"]:
            credentials.set_key(v["name"], v["api_key"])
        self._say(f"已添加 {v['name']}（建议点开「编辑」里的状态点确认，或直接设为默认）")
        self.refresh()