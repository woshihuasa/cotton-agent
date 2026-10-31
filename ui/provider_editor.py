# -*- coding: utf-8 -*-
"""服务商管理界面 —— 卡片式列表 + 编辑对话框。**模型（LLM）与 Embedding 共用同一套**。

【设计参照】DeepSeek Harness（DSH）的 `dsh-client-ui-settings-models`，以及用户提供的界面截图：
        **圆角卡片行 · 名称 + 状态点 + [编辑]/[删除] · 虚线「＋ 添加xxx」**。
        不用表格、不用表头 —— 表格在这里显得生硬，卡片更像"一列设置项"。

        照搬的四条原则：
          · **密钥只写、绝不回显** —— 输入框初始为空，保存后只显示掩码指纹（`sk-b…c42b`）；
          · **状态点"只有确认才显示"** —— 绿=已配置、红=未配置，不确定就不显示；
          · **内置项的端点只读** —— 端点里编着地域与计费类型，改错会得到"看着像密钥失效"的 401；
          · **测试连接区分四类失败** —— 因为"端点/空间不匹配"也报 401，错误信息会骗人。

        另加一条本项目必需的能力（截图里没有、但去掉旧配置栏后必须有）：
          · **「设为默认」** —— 决定 `LLM_PROVIDER` / `EMBEDDING_PROVIDER` 指向谁。
            没有它，用户就只能手工改 `.env`。

【`kind` 参数】★ 2026-11-01 把「Embedding 设置」改造成与「模型」同构：
  本模块的卡片、编辑对话框、状态点、测试连接**只有一份实现**，靠 `kind`
  切换后端（`core.providers` / `core.embedding_providers`）。两个后端刻意
  暴露**同名同签名**的 `all_specs / upsert_custom / delete_custom / list_models /
  key_name / default_provider / set_default`，界面因此不需要任何 `if kind`。

  为什么不去新写一套"长得很像"的控件：两套长得像的界面必然漂移
  （一边加了"测试连接填下拉"，另一边忘了），而用户看到的是"同一个功能有两种行为"。

【安全红线】本模块**绝不调用**取完整密钥的那个内部函数 ——
        `tests/check_credentials.py` 与 `tests/check_providers_ui.py` 有 AST 级断言盯着。
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from core import credentials, embedding_providers, model_cache, providers

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


# ---------------------------------------------------------------------------
# 两类服务商的**差异表** —— 界面代码里因此不需要任何 `if kind == ...`
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KindSpec:
    """一种"服务商列表页"的差异项（其余全部共用）。"""

    backend: object          # core.providers / core.embedding_providers（同名接口）
    noun: str                # 「模型提供商」/「Embedding 服务商」
    add_text: str            # 虚线添加按钮文字
    hint: str                # 卡片列表上方的一句话
    dialog_new: str          # 新增对话框标题
    default_text: str        # 编辑页勾选框文字
    default_done: str        # 设为默认后的提示（`{name}` 占位）
    footnote: str = ""       # 列表下方的一句灰字（可空）


KIND_SPECS: dict[str, KindSpec] = {
    "llm": KindSpec(
        backend=providers,
        noun="模型提供商",
        add_text="＋  添加模型提供商",
        hint="填入各提供商的 API 密钥即可使用其模型。密钥只写不读，保存后只显示掩码。",
        dialog_new="添加模型提供商",
        default_text="设为默认模型提供商",
        default_done="已把「{name}」设为默认模型提供商",
    ),
    "embedding": KindSpec(
        backend=embedding_providers,
        noun="Embedding 服务商",
        add_text="＋  添加 Embedding 服务商",
        hint="填入各服务商的 API 密钥即可用它生成向量。密钥只写不读，保存后只显示掩码。"
             "换服务商或模型会改变向量维度，保存后需要重建知识库。",
        dialog_new="添加 Embedding 服务商",
        default_text="设为默认 Embedding 服务商",
        default_done="已把「{name}」设为默认 Embedding 服务商",
        footnote="精排（rerank）默认沿用当前 Embedding 服务商的端点与密钥；"
                 "要单独指回某一家，可在 .env 里设置 RERANK_BASE_URL / RERANK_API_KEY。",
    ),
}


def kind_spec(kind: str) -> KindSpec:
    """按 kind 取差异表；未知 kind **明确报错**（不静默退化成 llm 页面）。"""
    try:
        return KIND_SPECS[kind]
    except KeyError:
        raise ValueError(
            f"未知的服务商类型 {kind!r}；可用: {sorted(KIND_SPECS)}") from None


def _status(name: str, kind: str = "llm") -> tuple[str, str]:
    """返回 (状态点颜色, 提示文字)。★ 只用 `has_key`/`fingerprint`，**不取完整密钥**。

    密钥键由后端 `key_name()` 决定（embedding 是 `embed:<名称>`），
    所以"同名 LLM 服务商配了 key、embedding 没配"这种情形不会互相冒充。
    """
    key = kind_spec(kind).backend.key_name(name)
    if not credentials.has_key(key):
        return COLOR_OFF, "未配置密钥"
    fp = credentials.fingerprint(key)
    src = credentials.source(key)
    where = "文件" if src == "file" else ("环境变量" if src == "env" else "")
    return COLOR_ON, f"{fp}" + (f"（{where}）" if where else "")


def default_provider_name(kind: str = "llm") -> str:
    """当前默认服务商名（`LLM_PROVIDER` / `EMBEDDING_PROVIDER`；空串=尚未指定）。

    ★ 空串在两边都意味着"沿用旧配置"：LLM 是 `DEEPSEEK_*`，Embedding 是
      `EMBEDDING_API_KEY/BASE_URL/MODEL`（见 `core/embedding_providers.resolve()`）。
    """
    return kind_spec(kind).backend.default_provider()


#: 模型下拉与它的候选浮层的样式。
#:
#: ★ 去掉原生箭头（`::drop-down` 宽度归零 + `::down-arrow` 无图），浮层改圆角白卡、
#:   行高与内边距放大、滚动条收细 —— 与主窗口里那个候选浮层（`PickerPopup`）
#:   同一套观感。原生 QComboBox 的弹出列表是**顶层窗口**，不设透明底就画不出圆角，
#:   所以下面还要给 `view().window()` 设 `WA_TranslucentBackground`（见 `ModelCombo`）。
MODEL_COMBO_QSS = """
QComboBox#modelCombo {
    background: #FFFFFF;
    border: 1px solid #E0E0E4;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    color: #1f2328;
}
QComboBox#modelCombo:hover { border-color: #C9CDD4; }
QComboBox#modelCombo:focus { border-color: #8AB8E8; }
QComboBox#modelCombo::drop-down { width: 0px; border: none; }
QComboBox#modelCombo::down-arrow { image: none; width: 0px; height: 0px; }

QComboBox#modelCombo QAbstractItemView {
    background: #FFFFFF;
    border: 1px solid #E3E5E8;
    border-radius: 8px;
    padding: 6px;
    outline: none;
    selection-background-color: #F2F3F5;
    selection-color: #1f2328;
}
QComboBox#modelCombo QAbstractItemView::item {
    min-height: 30px;
    padding: 4px 10px;
    border-radius: 6px;
    color: #1f2328;
}
QComboBox#modelCombo QAbstractItemView::item:hover { background: #F2F3F5; }
QComboBox#modelCombo QAbstractItemView::item:selected { background: #F2F3F5; }

QComboBox#modelCombo QScrollBar:vertical {
    background: transparent; width: 8px; margin: 4px 2px 4px 0;
}
QComboBox#modelCombo QScrollBar::handle:vertical {
    background: #D4D8DE; border-radius: 4px; min-height: 28px;
}
QComboBox#modelCombo QScrollBar::handle:vertical:hover { background: #BFC5CD; }
QComboBox#modelCombo QScrollBar::add-line:vertical,
QComboBox#modelCombo QScrollBar::sub-line:vertical { height: 0; }
QComboBox#modelCombo QScrollBar::add-page:vertical,
QComboBox#modelCombo QScrollBar::sub-page:vertical { background: transparent; }
"""


class _ModelFetch(QThread):
    """后台拉模型清单（`GET /models`）。**界面线程不碰网络。**"""

    done = pyqtSignal(bool, str, list)

    def __init__(self, kind: str, name: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._name = name

    def run(self) -> None:                                    # noqa: D102
        try:
            backend = kind_spec(self._kind).backend
            ok, msg, ids = backend.list_models(self._name)
        except Exception as e:                                # noqa: BLE001
            ok, msg, ids = False, f"{type(e).__name__}: {e}", []
        self.done.emit(bool(ok), str(msg), list(ids or []))


class ModelCombo(QComboBox):
    """可选可编辑的模型下拉：**打开就有候选**，缓存旧了在后台刷新。

    【为什么不是原生 QComboBox 直接给个列表】
      原生弹出列表是**顶层窗口**，样式与主界面格格不入（直角、系统滚动条、
      鼠标悬停的蓝底）；而本项目的候选浮层有一套既有观感（圆角白卡 + 细滚动条）。
      这里用 QSS 把字段与浮层都改成那套观感，并且去掉了原生箭头。

    【清单从哪来】
      构造时读 `core.model_cache` 的缓存（**不联网**，所以打开设置不会卡）；
      用户**点开下拉**那一刻，若缓存缺失或过期，起一个后台线程去拉，
      拉回来就地刷新。这样"配置过一次之后随时能选"，而不是每次先点「测试连接」。
    """

    def __init__(self, kind: str = "llm", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("modelCombo")
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setStyleSheet(MODEL_COMBO_QSS)
        self.setToolTip("打开即可选择该端点提供的模型；也可以直接输入型号名。"
                        "清单缓存自上次拉取，点开时会自动刷新。")
        if self.lineEdit() is not None:
            self.lineEdit().setPlaceholderText(
                "点这里选择模型，或直接输入，例如 "
                + ("BAAI/bge-large-zh-v1.5" if kind == "embedding" else "qwen3.7-plus"))
        self._kind = kind
        self._provider = ""
        self._fetch: _ModelFetch | None = None

        # ★ 浮层是顶层窗口：要圆角就必须让窗口四角透明，否则会渲染出黑块。
        #   光设 `WA_TranslucentBackground` 还不够 —— 承载列表的那个容器
        #   （`QComboBoxPrivateContainer`）自己会刷一层不透明底，把圆角盖成直角。
        #   所以容器也要显式透明，圆角由列表自身的 QSS 画。
        view = self.view()
        view.setStyleSheet(MODEL_COMBO_QSS)
        win = view.window()
        if win is not None:
            win.setWindowFlags(Qt.WindowType.Popup
                               | Qt.WindowType.FramelessWindowHint
                               | Qt.WindowType.NoDropShadowWindowHint)
            win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            win.setStyleSheet("background: transparent; border: none;")

    # ---- 数据 ----

    def set_provider(self, kind: str, name: str) -> None:
        """告诉它"现在编辑的是哪个服务商"，后台刷新才知道向谁要清单。"""
        self._kind = kind or self._kind
        self._provider = str(name or "")

    def set_models(self, ids) -> None:
        """填入候选清单，**保留当前填的名字**（不清空、不强制改选）。"""
        current = self.currentText().strip()
        self.blockSignals(True)
        self.clear()
        self.addItems([str(i) for i in (ids or []) if str(i).strip()])
        if current:
            self.setCurrentText(current)
        self.blockSignals(False)

    def models(self) -> list[str]:
        return [self.itemText(i) for i in range(self.count())]

    # ---- 打开下拉时按需刷新 ----

    def showPopup(self) -> None:                              # noqa: N802
        """点开那一刻决定要不要刷新；**刷新在后台**，不挡着选。

        ★ 只在"有服务商名 + 缓存缺失或过期"时才拉。新建直接弹出的服务商还没落库、
          没有密钥，拉也拉不到，反而会白等一次超时。
        """
        if (self._provider
                and model_cache.is_stale(self._kind, self._provider)
                and not (self._fetch is not None and self._fetch.isRunning())):
            self._fetch = _ModelFetch(self._kind, self._provider, parent=self)
            self._fetch.done.connect(self._on_fetched)
            self._fetch.start()
        super().showPopup()

    def _on_fetched(self, ok: bool, _msg: str, ids: list) -> None:
        if ok and ids:
            self.set_models(ids)


class ProviderEditDialog(QDialog):
    """新增/编辑一个服务商（`kind` 决定是模型还是 Embedding）。

    · **内置**项：`id` 与 `接口地址` **只读**（端点里编着地域与计费类型，
      改错会返回 401 且错误信息**看着像密钥失效**）；
    · **自定义**项：字段全可编辑，但端点**必须是 https**。
    """

    def __init__(self, spec: dict, *, is_new: bool = False, parent=None,
                 kind: str = "llm") -> None:
        super().__init__(parent)
        self.kind = kind
        self._ks = kind_spec(kind)
        self.spec = dict(spec)
        self.is_new = is_new
        self.builtin = bool(spec.get("builtin"))
        self.setWindowTitle((self._ks.dialog_new if is_new else "编辑")
                            + f" · {spec.get('label', '')}")
        self.setMinimumWidth(540)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 内部 id 创建后锁定：它是配置与日志的键
        self._id = QLineEdit(spec.get("name", ""))
        self._id.setPlaceholderText("仅小写字母/数字/_/-，例如 my-gateway")
        self._id.setEnabled(is_new)
        if not is_new:
            self._id.setToolTip("内部 id 创建后不可修改")
        form.addRow(QLabel("内部 id:"), self._id)

        self._label = QLineEdit(spec.get("label", ""))
        self._label.setPlaceholderText("例如 Deepseek")
        form.addRow(QLabel("供应商名称:"), self._label)

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

        # ★ 默认模型：**可选可编辑下拉**，清单来自缓存（`core/model_cache`），
        #   所以**打开对话框就有候选可点**，不必先点「测试连接」。
        #   缓存缺/旧时在**打开下拉那一刻**后台刷新（见 `showPopup`），不阻塞界面；
        #   构造对话框全程**不发网络请求**（否则开设置会卡，守卫也会变成联网测试）。
        self._model = ModelCombo(kind)
        self._model.set_provider(kind, str(spec.get("name") or ""))
        self._model.setCurrentText(str(spec.get("default_model", "") or ""))
        self._model.set_models(model_cache.get(kind, str(spec.get("name") or "")))
        form.addRow(QLabel("默认模型:"), self._model)

        # API 密钥：只写。留空 = 不修改
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("留空 = 不修改现有密钥")
        form.addRow(QLabel("API 密钥:"), self._key)

        color, text = _status(spec.get("name", ""), kind)
        self._status = QLabel(f'<span style="color:{color}">{DOT_OFF}</span> 当前：{text or "未配置"}')
        form.addRow(QLabel(""), self._status)

        # ★ 「设为默认」放在编辑页里 —— 卡片上保持截图那种只有 [编辑]/[删除] 的干净样子
        self._make_default = QCheckBox(self._ks.default_text)
        self._make_default.setChecked(default_provider_name(kind) == spec.get("name"))
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
            "default_model": self._model.currentText().strip(),
            "api_key": self._key.text(),          # 空串 = 不修改
            "make_default": self._make_default.isChecked(),
        }

    def _on_test(self) -> None:
        """测试连接 —— 调 `GET /models`（**不消耗生成 token**），并区分四类失败。

        ★ 顺便把模型清单填进下拉框，并检查用户填的默认模型**在不在清单里**：
          名字抄错一个字符时，错误原本要到第一次生成才暴露，而现在当场就能看见。
        """
        name = self.spec.get("name", "")
        if not name:
            return
        self._btn_test.setEnabled(False)          # 进行中锁定，防重复点击
        self._test_msg.setText("正在测试连接…")
        self._test_msg.setStyleSheet("font-size:11px; color:#8a919b;")
        try:
            ok, detail, ids = self._ks.backend.list_models(name)
        finally:
            self._btn_test.setEnabled(True)

        if ok and ids:
            self._fill_models(ids)
            want = self._model.currentText().strip()
            if want and want not in ids:
                detail += f"；⚠️ 但清单里没有「{want}」这个模型，可能拼错了"
                ok = False

        self._test_msg.setText(("✓ " if ok else "✗ ") + detail)
        self._test_msg.setStyleSheet("font-size:11px; color:%s;"
                                     % (COLOR_ON if ok else COLOR_OFF))

    def _fill_models(self, ids: list[str]) -> None:
        """把模型清单填进下拉框（保留当前填的名字）。逻辑在 `ModelCombo.set_models`。"""
        self._model.set_models(ids)

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
    """一行服务商：名称 + 状态点 + [编辑] (+ [删除])。"""

    def __init__(self, spec: dict, *, is_default: bool, parent_group: "ProviderGroup") -> None:
        super().__init__(parent_group)
        self.spec = spec
        self.name = spec["name"]
        self.group = parent_group
        self.kind = parent_group.kind
        self.setObjectName("providerCard")
        # ★ 样式表由 `ProviderGroup` 统一下发（`_CARD_QSS`），卡片**不再各自带一份**：
        #   那样写的话「＋ 添加」按钮的 `#addProvider` 规则永远匹配不到，
        #   因为 `btn_add` 不是卡片的子控件 —— 虚线框因此消失，按钮只剩一行灰字。

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

        color, tip = _status(self.name, self.kind)
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
    """一组服务商：卡片列表 + 添加按钮 + 状态提示。

    `kind="llm"` → 「模型」页；`kind="embedding"` → 「Embedding」页。
    """

    # ★ 三个独立赋值而不是元组解包：`tests/check_ui_attrs.py` 只认**单个 Name 目标**
    #   的类级赋值，元组目标会被当成"有读无定义"（那守卫会误报）。
    #: 卡片行高（`refresh()` 量不到真实高度时的兜底估算）
    ROW_H: int = 44
    #: 卡片列表下方的余量
    PAD: int = 6
    #: 卡片列表滚动区的最大高度（再多就出滚动条）
    MAX_H: int = 320

    def __init__(self, parent=None, *, kind: str = "llm") -> None:
        super().__init__(parent)
        self.kind = kind
        self._ks = kind_spec(kind)
        # ★ 样式表下发在**分组**这一层：卡片与「＋ 添加」按钮都在它的子树里。
        #   放在卡片上会让 `QPushButton#addProvider` 规则永远匹配不到。
        self.setStyleSheet(_CARD_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        hint = QLabel(self._ks.hint)
        hint.setObjectName("formLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # 卡片列表放在滚动区里：服务商多了也不会把对话框撑爆
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setMaximumHeight(self.MAX_H)
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

        self.btn_add = QPushButton(self._ks.add_text)
        self.btn_add.setObjectName("addProvider")
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.clicked.connect(self._on_add)
        root.addWidget(self.btn_add)

        self._msg = QLabel("")
        self._msg.setWordWrap(True)
        self._msg.setStyleSheet("font-size:11px;")
        root.addWidget(self._msg)

        if self._ks.footnote:
            note = QLabel(self._ks.footnote)
            note.setObjectName("formLabel")
            note.setWordWrap(True)
            note.setStyleSheet("color:#8a919b; font-size:11px;")
            root.addWidget(note)

        self.refresh()

    # ---- 列表 ----
    def refresh(self) -> None:
        while self._list.count():
            it = self._list.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        specs = self._ks.backend.all_specs()
        cur = default_provider_name(self.kind)
        for name, spec in specs.items():
            self._list.addWidget(ProviderCard(spec, is_default=(name == cur),
                                              parent_group=self))
        self._list.addStretch(1)
        # ★ 让滚动区**贴合卡片数量**，而不是固定 320 高 ——
        # 固定高度会让 2 家服务商时下方留一大片空白（离屏预览时最明显）。
        # 优先用**真实**内容高度（sizeHint），量不到才退回按行数估算。
        content = self._body.sizeHint().height()
        if content <= 0:
            content = len(specs) * self.ROW_H
        h = min(content + self.PAD, self.MAX_H)
        self._scroll.setFixedHeight(max(h, self.ROW_H))

    def _say(self, text: str, *, error: bool = False) -> None:
        self._msg.setText(text)
        self._msg.setStyleSheet("font-size:11px; color:%s;"
                                % ("#d4380d" if error else "#8a919b"))

    # ---- 动作（供卡片回调）----
    def edit_provider(self, name: str) -> None:
        spec = self._ks.backend.all_specs().get(name)
        if spec is None:
            return
        dlg = ProviderEditDialog(spec, is_new=False, parent=self, kind=self.kind)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if v["api_key"]:
            # ★ 密钥键由后端决定 —— embedding 是 `embed:<名称>`，不与同名 LLM 冲突
            credentials.set_key(self._ks.backend.key_name(name), v["api_key"])
        try:
            if spec.get("builtin"):
                # ★ 内置项：端点固定不可改，但**显示名 / 默认模型必须能存下来**。
                #   旧实现只给自定义项调 upsert_custom()，内置项的修改被**静默丢弃**
                #   （用户以为换了模型，实际没换，直到下次调用才暴露）。
                self._ks.backend.update_builtin(
                    name, label=v["label"], default_model=v["default_model"])
            else:
                self._ks.backend.upsert_custom(name, label=v["label"],
                                               base_url=v["base_url"],
                                               default_model=v["default_model"])
        except ValueError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        if v["make_default"] and name != default_provider_name(self.kind):
            self.set_default(name)
            return                       # set_default 内部已 refresh 并提示
        self._say(f"已保存 {name}" + ("（密钥未改动）" if not v["api_key"] else ""))
        self.refresh()

    def set_default(self, name: str) -> None:
        """把某个服务商设为默认 —— 写 `<LLM|EMBEDDING>_PROVIDER` 到 `.env` 并热更新。

        ★ 真正落盘的逻辑在核心层（`core.providers.set_default` /
          `core.embedding_providers.set_default`）：界面只调用与提示。
          这样"设为默认"的往返**不依赖 Qt** 也能被守卫验证（不必为了测一行
          `.env` 写入去跑事件循环）。
        """
        try:
            self._ks.backend.set_default(name)
        except Exception as e:                               # noqa: BLE001
            self._say(f"设为默认失败：{type(e).__name__}: {e}", error=True)
            self.refresh()
            return
        self._say(self._ks.default_done.format(name=name))
        self.refresh()

    def delete_provider(self, name: str) -> None:
        ok = QMessageBox.question(
            self, f"删除{self._ks.noun}",
            f"确定删除「{name}」吗？它的密钥也会一并清除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            self._ks.backend.delete_custom(name)
        except ValueError as e:
            QMessageBox.warning(self, "删除失败", str(e))
            return
        self._say(f"已删除 {name}")
        self.refresh()

    def _on_add(self) -> None:
        spec = {"name": "", "label": "", "base_url": "", "default_model": "", "builtin": False}
        dlg = ProviderEditDialog(spec, is_new=True, parent=self, kind=self.kind)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        try:
            self._ks.backend.upsert_custom(v["name"], label=v["label"],
                                           base_url=v["base_url"],
                                           default_model=v["default_model"])
        except ValueError as e:
            QMessageBox.warning(self, "添加失败", str(e))
            return
        if v["api_key"]:
            credentials.set_key(self._ks.backend.key_name(v["name"]), v["api_key"])
        self._say(f"已添加 {v['name']}（建议点开「编辑」里的状态点确认，或直接设为默认）")
        self.refresh()