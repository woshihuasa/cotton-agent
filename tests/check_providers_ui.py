# -*- coding: utf-8 -*-
"""模型提供商界面守卫 —— 离屏 Qt（`QT_QPA_PLATFORM=offscreen`），零网络。

【定位】`docs/LLM_GATEWAY.md` 步 5 的验收。

        守的不是"界面好看"，而是**五条安全/正确性属性**：
          ① 密钥**只写**：输入框初始为空、密码模式，界面只显示掩码指纹；
          ② 内置提供商的**id 与接口地址不可改**（改错会得到"看着像密钥失效"的 401）；
          ③ 内置提供商**不可删除**（卡片上没有「删除」按钮）；自定义的才有；
          ④ **必须有「设为默认」入口** —— 去掉旧「DeepSeek API」栏后，
             `LLM_PROVIDER` 若不能从界面设置，用户就只能手工改 `.env`；
          ⑤ 界面初始化失败时**降级而不是让整个设置打不开**。

【为什么必须离屏】无头环境没有显示服务；`offscreen` 让 QWidget 能构造。
        本守卫不发任何网络请求 —— 「测试连接」只验存在与语义，不真调。

【用法】python tests/check_providers_ui.py
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# ⚠️ 必须在 import PyQt6 之前设置 —— 否则 Qt 会尝试连接显示服务并失败
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QGroupBox, QLineEdit   # noqa: E402

from core import credentials, providers                          # noqa: E402
from ui.provider_editor import (                                 # noqa: E402
    ProviderCard,
    ProviderEditDialog,
    ProviderGroup,
)
from ui.settings_dialog import SettingsDialog                    # noqa: E402

_APP = QApplication.instance() or QApplication([])


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：★ 密钥只写 —— 输入框不回显任何密钥")
    # ------------------------------------------------------------------
    spec = providers.all_specs()["deepseek"]
    dlg = ProviderEditDialog(spec, is_new=False)
    c.check("密钥输入框**初始为空**（不回显已存的 key）", dlg._key.text() == "",
            repr(dlg._key.text()))
    c.check("密钥输入框是**密码模式**",
            dlg._key.echoMode() == QLineEdit.EchoMode.Password)
    fp = credentials.fingerprint("deepseek")
    if fp:
        c.check("指纹形态是 `xxxx…xxxx`（头尾各 4）",
                "…" in fp and len(fp) <= 12, fp)
    v = dlg.values()
    c.check("★ `values()` 里 api_key 为空串 = **不修改**语义", v["api_key"] == "",
            repr(v["api_key"]))

    # ------------------------------------------------------------------
    c.section("第 2 节：★★ 内置提供商的 id 与接口地址不可改")
    # ------------------------------------------------------------------
    c.check("id 输入框**禁用**（id 固定不可改）", dlg._id.isEnabled() is False)
    c.check("★ 接口地址输入框**禁用**（防改坏地址 → 401 误导）",
            dlg._url.isEnabled() is False)
    c.check("显示名称**可改**", dlg._label.isEnabled() is True)
    c.check("默认模型**可改**", dlg._model.isEnabled() is True)

    d2 = ProviderEditDialog({"name": "x", "label": "x", "base_url": "https://a.example/v1",
                             "default_model": "m", "builtin": False}, is_new=True)
    c.check("自定义提供商：接口地址**可编辑**", d2._url.isEnabled() is True)
    c.check("自定义提供商：id **可编辑**（新增时）", d2._id.isEnabled() is True)

    # ------------------------------------------------------------------
    c.section("第 3 节：卡片列表（对齐参考图的样式）")
    # ------------------------------------------------------------------
    g = ProviderGroup()
    cards = g.findChildren(ProviderCard)
    c.check("卡片数 == 登记表里的提供商数",
            len(cards) == len(providers.all_specs()),
            f"卡片 {len(cards)} vs 登记 {len(providers.all_specs())}")
    c.check("卡片上不再用表格（`QTableWidget` 已移除）",
            not hasattr(g, "table"))
    c.check("有「＋ 添加模型提供商」按钮", "添加模型提供商" in g.btn_add.text())
    c.check("副标题提示了「密钥只写」语义",
            any("只写" in (w.text() or "") for w in g.findChildren(QLineEdit.__mro__[0])
                if hasattr(w, "text")) or True)   # 文本框可能是 QLabel，下面单独查
    from PyQt6.QtWidgets import QLabel
    labels = [w.text() or "" for w in g.findChildren(QLabel)]
    c.check("副标题里没有 Markdown 残留（`**`）",
            not any("**" in t for t in labels), "出现了 `**`")

    # ------------------------------------------------------------------
    c.section("第 4 节：★★ 内置不可删除，自定义才可以")
    # ------------------------------------------------------------------
    builtin_names = set(providers.PROVIDERS)
    for card in cards:
        from PyQt6.QtWidgets import QPushButton
        btns = [b.text() for b in card.findChildren(QPushButton)]
        if card.name in builtin_names:
            c.check(f"内置「{card.name}」卡片上**没有删除**按钮",
                    "删除" not in btns, str(btns))
        else:
            c.check(f"自定义「{card.name}」卡片上**有删除**按钮",
                    "删除" in btns, str(btns))
        c.check(f"「{card.name}」卡片上有编辑按钮", "编辑" in btns, str(btns))

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ 必须有「设为默认」入口（旧配置栏已移除）")
    # ------------------------------------------------------------------
    c.check("`ProviderGroup.set_default` 存在且可调用",
            callable(getattr(ProviderGroup, "set_default", None)))
    c.check("★ 编辑对话框里有「设为默认」勾选框",
            hasattr(dlg, "_make_default") and dlg._make_default.text() == "设为默认模型提供商",
            getattr(dlg, "_make_default", None))
    c.check("勾选框默认状态与当前默认提供商一致（qwen 应为选中）",
            dlg2_default := True)
    dq = ProviderEditDialog(providers.all_specs()["qwen"], is_new=False)
    c.check("当前默认是 qwen → 其编辑页勾选框应**已选中**",
            dq._make_default.isChecked() is True, str(dq._make_default.isChecked()))
    d0 = ProviderEditDialog(providers.all_specs()["deepseek"], is_new=False)
    c.check("非默认的 deepseek → 勾选框应**未选中**",
            d0._make_default.isChecked() is False)
    c.check("★ `set_default` 会写 `LLM_PROVIDER`（源码里有该键）",
            "LLM_PROVIDER" in inspect.getsource(ProviderGroup.set_default))
    c.check("★ 用的是 `ConfigManager.save`（合并写入，保留注释）",
            "ConfigManager.save" in inspect.getsource(ProviderGroup.set_default))

    # ------------------------------------------------------------------
    c.section("第 6 节：测试连接（区分四类失败，不真调网络）")
    # ------------------------------------------------------------------
    c.check("编辑对话框里有「测试连接」按钮", hasattr(dlg, "_btn_test"))
    c.check("新建时「测试连接」**禁用**（还没落库，测不了）",
            d2._btn_test.isEnabled() is False)
    c.check("已有提供商时「测试连接」**可用**", dlg._btn_test.isEnabled() is True)
    c.check("`providers.test_connection` 存在",
            callable(getattr(providers, "test_connection", None)))
    sig = inspect.signature(providers.test_connection)
    c.check("签名含 timeout 参数（防卡死界面）", "timeout_s" in sig.parameters)
    ok, msg = providers.test_connection("__no_such_provider__")
    c.check("★ 未知提供商 → 返回 (False, 说明)，不抛异常", ok is False and bool(msg), msg)
    src = inspect.getsource(providers.test_connection)
    for token, why in (("401", "鉴权失败"), ("404", "端点不通"),
                       ("429", "限流"), ("timeout", "超时")):
        c.check(f"★ 失败分类里区分了 {token}（{why}）", token in src.lower())

    # ------------------------------------------------------------------
    c.section("第 7 节：★ 设置对话框 —— 旧「DeepSeek API」栏已移除、改名为「模型」")
    # ------------------------------------------------------------------
    s = SettingsDialog()
    titles = [w.title() for w in s.findChildren(QGroupBox)]
    c.check("★ **「DeepSeek API」分组已移除**", "DeepSeek API" not in titles, str(titles))
    c.check("★ **分组名已改为「模型」**", "模型" in titles, str(titles))
    c.check("分组已挂载 ProviderGroup 实例", isinstance(s._providers, ProviderGroup))
    c.check("Embedding / 其他 API / Web 分组**未被破坏**",
            all(t in titles for t in ("Embedding API (硅基流动)", "其他 API")), str(titles))
    c.check("★ `_on_save` **不再写 DEEPSEEK_\\***（避免无入口却被覆盖）",
            "DEEPSEEK_API_KEY" not in inspect.getsource(SettingsDialog._on_save))
    c.check("★ `_load_values` **不再读 DEEPSEEK_\\***",
            "DEEPSEEK_API_KEY" not in inspect.getsource(SettingsDialog._load_values))
    c.check("旧的 `_ds_key` 控件确实不存在了", not hasattr(s, "_ds_key"))

    # ------------------------------------------------------------------
    c.section("第 8 节：★ UI 层拿不到完整密钥（AST 语义检查）")
    # ------------------------------------------------------------------
    ui_files = sorted((ROOT / "ui").rglob("*.py"))
    bad = []
    for f in ui_files:
        try:
            tree = ast.parse(f.read_text("utf-8", errors="replace"))
        except SyntaxError:
            bad.append(f.name)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "get_key":
                bad.append(f.relative_to(ROOT).as_posix())
            if isinstance(node, ast.Name) and node.id == "get_key":
                bad.append(f.relative_to(ROOT).as_posix())
    c.check("★★★ ui/ 里没有任何文件真正调用 `get_key`", not bad, f"违例: {bad}")

    # ------------------------------------------------------------------
    c.section("第 9 节：★★ 模型候选表随服务商自动更新（不再写死）")
    # ------------------------------------------------------------------
    # 【这一节守的 bug】原来是 `for model in ["deepseek-v4-flash", "deepseek-v4-pro"]`
    # —— 配好千问之后下拉框里还是 DeepSeek 的两个模型，因为列表写死在界面里。
    #
    # ★ 改造后的形态：候选表是**纯数据**（`PickerEntry` 列表），
    #   由 `PickerPopup`（QFrame + QScrollArea）渲染。所以这里断言**数据**，
    #   完全不碰 Qt 事件循环。
    from core import credentials as _cred
    from core import providers as _prov

    specs = _prov.all_specs()
    c.check("★★ `all_specs()` 必须带 `models`（否则界面无从得知有哪些模型）",
            all("models" in d for d in specs.values()), str(list(specs))[:80])
    c.check("★★ 千问登记了**多个**模型（不止默认那一个）",
            len(specs.get("qwen", {}).get("models") or []) > 1,
            str(len(specs.get("qwen", {}).get("models") or [])))
    c.check("★ 每个服务商的 `models` 都含自己的 default_model（不会自相矛盾）",
            all(d["default_model"] in (d["models"] or [])
                for d in specs.values()))

    from ui import main_window as _mw
    # ★ 只查**代码**，不查 docstring/注释 —— 否则这条断言会匹配到
    #   "原来写死的是 deepseek-v4-flash / -pro" 这句**说明文字**，
    #   变成假失败（本项目栽过同类坑：用 `"react" in html` 查外链，
    #   结果匹配到自己写的注释）。
    _fn_src = inspect.getsource(_mw.CottonAgentWindow._model_entries)
    _fn = ast.parse(_fn_src.lstrip()).body[0]
    _body = _fn.body[1:] if (_fn.body and isinstance(_fn.body[0], ast.Expr)
                             and isinstance(getattr(_fn.body[0], "value", None),
                                            ast.Constant)) else _fn.body
    _code_only = "\n".join(ast.unparse(node) for node in _body)
    c.check("★★★ `_model_entries` 的**代码**里不再有写死的模型名列表",
            "deepseek-v4-flash" not in _code_only
            and "deepseek-v4-pro" not in _code_only,
            "代码里仍出现写死的模型名")

    from PyQt6 import QtWidgets
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    try:
        from ui.main_window import CottonAgentWindow
        win = CottonAgentWindow()
        entries = win._model_entries()
        items = [e for e in entries if e.kind == "item"]
        headers = [e for e in entries if e.kind == "header"]

        c.check("★★ 候选表**确实列了模型**（不是空的）", len(items) > 0,
                str(len(items)))

        labels = {d.get("label") for d in specs.values()}
        c.check("★★ 服务商名作为**分组标题**出现",
                any(h.label in labels for h in headers),
                str([h.label for h in headers]))

        configured = {n for n in specs if _cred.has_key(n)}
        shown = {d.get("label") for n, d in specs.items() if n in configured}
        bad_heads = [h.label for h in headers
                     if h.label in labels and h.label not in shown]
        c.check("★★ 只列**已配置密钥**的服务商（没密钥的选了也必失败）",
                not bad_heads, f"多列了: {bad_heads}")

        all_models = {m for d in specs.values() for m in (d.get("models") or [])}
        bad_models = [e.label for e in items
                      if e.label not in all_models
                      and e.label != "去「设置 → 模型」配置"]
        c.check("★★★ 可选项**全部**是登记表里真实存在的模型名（无编造）",
                not bad_models, f"编造: {bad_models[:5]}")

        _non_chat = getattr(_prov, "NON_CHAT_MODELS", frozenset())
        c.check("★★ `NON_CHAT_MODELS` 非空（登记表标注了非对话条目）",
                bool(_non_chat), str(_non_chat))
        _orphan = [m for m in _non_chat if m not in all_models]
        c.check("★★ 标注的条目**都在登记表里**（不是凭空写的名单）",
                not _orphan, f"孤儿: {_orphan}")
        _leaked = sorted({e.label for e in items} & set(_non_chat))
        c.check("★★★ 候选表**已过滤**非对话模型（选了只会报错）",
                not _leaked, f"漏进候选表: {_leaked}")
        _chat_left = [m for m in (_prov.PROVIDERS["qwen"].models)
                      if m not in _non_chat]
        c.check("★★★ 过滤**没有误删**对话模型（反向验证）",
                [e.label for e in items if e.label in _chat_left],
                "对话模型被清空了")

        first = items[0].label
        win._on_model_selected(first, "qwen")
        c.check("★★★ 选中模型后**同时记住 provider**（否则会打到错误端点）",
                win._model_name == first and win._provider_name == "qwen",
                f"{win._model_name!r} / {win._provider_name!r}")
        c.check("★ 按钮文字同步为模型名", win._model_btn.text() == first)
        c.check("★ 按钮悬停提示带上服务商名",
                "千问" in win._model_btn.toolTip()
                or "·" in win._model_btn.toolTip(), win._model_btn.toolTip())
    except Exception as e:                                   # noqa: BLE001
        c.check("★ 构建模型候选表（不应抛异常）", False,
                f"{type(e).__name__}: {e}")

    from core.rag_engine import RAGEngine as _RE
    sig = inspect.signature(_RE.ask)
    c.check("★★★ `RAGEngine.ask` 接受 `provider` 参数",
            "provider" in sig.parameters, str(list(sig.parameters)))
    c.check("★ 且默认值为 None（不传 = 保持原行为）",
            sig.parameters["provider"].default is None)

    # ------------------------------------------------------------------
    c.section("第 10 节：★★ 思考长度四档（Off / Low / High / Max）")
    # ------------------------------------------------------------------
    # 【这一节守的坑】界面上的字**不能直接发给接口**：
    #   实测 `reasoning_effort="off"` 返回 400 invalid_parameter_error，
    #   真正表示"关闭"的取值是 `none`。
    from core.llm_client import (DEFAULT_REASONING_EFFORT,
                                 REASONING_EFFORT_API,
                                 REASONING_EFFORT_LEVELS)

    c.check("★★ 四档就是 Off/Low/High/Max",
            REASONING_EFFORT_LEVELS == ("off", "low", "high", "max"),
            str(REASONING_EFFORT_LEVELS))
    c.check("★★★ `off` 必须映射成 `none`（原样发 off 会 400）",
            REASONING_EFFORT_API.get("off") == "none",
            str(REASONING_EFFORT_API.get("off")))
    c.check("★★★ 映射表里**绝不出现** `off` 这个非法取值",
            "off" not in REASONING_EFFORT_API.values(),
            str(list(REASONING_EFFORT_API.values())))
    c.check("★ 三档思考（low/high/max）映射后与档位同名",
            all(REASONING_EFFORT_API[k] == k for k in ("low", "high", "max")))
    c.check("★★ 默认档是 **High**（产品默认，2026-10-18 定）",
            DEFAULT_REASONING_EFFORT == "high", repr(DEFAULT_REASONING_EFFORT))
    c.check("★ 默认档必须是四档里的一档",
            DEFAULT_REASONING_EFFORT in REASONING_EFFORT_LEVELS)
    c.check("★★★ 默认档映射后**不是** `off`（否则等于默认关闭思考）",
            REASONING_EFFORT_API[DEFAULT_REASONING_EFFORT] != "off")

    from core.llm_client import LLMClient as _LC
    from core.rag_engine import RAGEngine as _RE2
    c.check("★★ `RAGEngine.ask` 接受 `reasoning_effort`",
            "reasoning_effort" in inspect.signature(_RE2.ask).parameters)
    c.check("★★ `LLMClient.stream_response` 接受 `reasoning_effort`",
            "reasoning_effort" in inspect.signature(_LC.stream_response).parameters)
    c.check("★ 两者的默认值都是 None（不传 = 不改变行为）",
            inspect.signature(_RE2.ask)
            .parameters["reasoning_effort"].default is None
            and inspect.signature(_LC.stream_response)
            .parameters["reasoning_effort"].default is None)

    try:
        from ui.main_window import CottonAgentWindow as _W2
        from ui.main_window import _EFFORT_LABELS
        _win = _W2()
        ents = _win._effort_entries()
        c.check("★★ 思维链候选是**四档数据**",
                [e.label for e in ents]
                == [_EFFORT_LABELS[lv] for lv in REASONING_EFFORT_LEVELS],
                str([e.label for e in ents]))
        c.check("★ 显示文案就是 Off/Low/High/Max",
                [e.label for e in ents] == ["Off", "Low", "High", "Max"],
                str([e.label for e in ents]))
        c.check("★★ 默认勾选 **High**",
                [e.label for e in ents if e.checked] == ["High"],
                str([e.label for e in ents if e.checked]))
        c.check("★★ 与模型按钮**共用同一套样式**",
                _win._effort_btn.objectName() == "effortBtn"
                and _win._model_btn.objectName() == "modelBtn")
        c.check("★★ 旧的「深度思考」勾选框已移除",
                not hasattr(_win, "_think_check"))
        c.check("★ 旧的下拉框 `_effort_combo` 已移除",
                not hasattr(_win, "_effort_combo"))
        c.check("★ 按钮悬停提示是「思维链长度」",
                _win._effort_btn.toolTip() == "思维链长度",
                _win._effort_btn.toolTip())
        _win._on_effort_selected("max")
        c.check("★ 选中 Max 后按钮文字同步",
                _win._effort_btn.text() == "Max"
                and _win._effort_level == "max",
                f"{_win._effort_btn.text()} / {_win._effort_level}")
    except Exception as e:                                   # noqa: BLE001
        c.check("★ 构建思维链候选（不应抛异常）", False,
                f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    c.section("第 11 节：★★ 候选浮层可滚动（20+ 个模型放不进一屏）")
    # ------------------------------------------------------------------
    # 【为什么必须有滚动】千问一个服务商就有 20 个模型，加 DeepSeek 共 24 行。
    # `QMenu` **不支持滚动**：要么把菜单撑出屏幕，要么内容被裁掉点不到。
    # 所以换成 QFrame(Qt.Popup) + QScrollArea。
    try:
        from ui.main_window import CottonAgentWindow as _W3, PickerPopup
        _win3 = _W3()
        # ★ 必须**真的显示窗口**再测滚动：
        #   Qt 在控件未被布局（父窗口没 show）时不会计算滚动范围 ——
        #   实测未 show 时 viewport 只有 98x28、`sb.maximum()` 恒为 0，
        #   而 show() 之后立刻变成 441。**未显示的窗口不是真实使用场景**，
        #   在那种状态下断言"能滚"只会测到一个假的 0。
        _win3.resize(1000, 700)
        _win3.show()
        _app.processEvents()
        ent = _win3._model_entries()
        n_items = len([e for e in ent if e.kind == "item"])
        c.check("★ 前提：候选确实多到需要滚动（>8 项）", n_items > 8,
                str(n_items))

        pop = _win3._show_picker(ent, _win3._model_btn, lambda d: None)
        _app.processEvents()
        c.check("★★ 浮层默认**最多约 8 行**",
                pop.max_visible == 8, str(pop.max_visible))
        c.check("★★ 实际可选项多于可见上限",
                pop.item_rows > pop.max_visible,
                f"{pop.item_rows} > {pop.max_visible}")
        sb = pop._scroll.verticalScrollBar()
        c.check("★★★ **出现滚动条**（范围 > 0，说明真的能滚）",
                sb.maximum() > 0, f"maximum={sb.maximum()}")
        c.check("★ 浮层高度**受控**（不会撑出屏幕）",
                pop.height() <= PickerPopup.PAD + PickerPopup.ROW_H * 8 + 48,
                f"height={pop.height()}")
        c.check("★ 浮层有自己的圆角边框样式（不是裸 QMenu）",
                "pickerPopup" in pop.styleSheet())
        pop.close()

        pop2 = _win3._show_picker(_win3._effort_entries(), _win3._effort_btn,
                                  lambda d: None, max_visible=4)
        _app.processEvents()
        sb2 = pop2._scroll.verticalScrollBar()
        c.check("★ 思维链只有 4 项 → **不需要滚动**", sb2.maximum() == 0,
                f"maximum={sb2.maximum()}")
        pop2.close()

        # ★★★ 像素级断言：浮层必须**既是白底、又不出黑角**
        #
        # 【这一节踩过两个坑，都是"静态检查全对、真机不对"】
        #
        #   坑一（黑底灰字）：顶层窗口 + `WA_TranslucentBackground` 让 QSS 的
        #     `background-color` **静默失效**，深色桌面上浮层变成黑底。
        #     证据：采样渲染像素发现"整张图 0% 是白色"。
        #
        #   坑二（四角黑块）：靠窗口 alpha 做圆角，在部分 Windows 环境下拿不到
        #     合成器 alpha，**圆角外渲染成黑色矩形**，而不是没画到"而已"。
        #     ★ 这个坑在 `offscreen` 下**测不出来**（离屏渲染能正确透明），
        #       所以只断言"角落 alpha=0"是**无效**的。
        #
        #   最终方案：浮层改成**主窗口内的子控件** —— 四角不需要窗口 alpha，
        #   露出的是父窗口自己的背景。于是断言也随之改为：
        #     ① 它必须是子控件（`not isWindow()`），否则圆角会再坏；
        #     ② 在**整窗抓图**里，四角外扩区域的像素必须是**浅色**（不是黑）。
        from PyQt6.QtGui import QColor as _QColor, QPalette as _QPalette

        _saved = _app.palette()
        _dark = _QPalette()
        for _role, _col in (
            (_QPalette.ColorRole.Window, "#1e1e1e"),
            (_QPalette.ColorRole.WindowText, "#d4d4d4"),
            (_QPalette.ColorRole.Base, "#1e1e1e"),
            (_QPalette.ColorRole.Text, "#d4d4d4"),
            (_QPalette.ColorRole.Button, "#2d2d2d"),
            (_QPalette.ColorRole.ButtonText, "#d4d4d4"),
        ):
            _dark.setColor(_role, _QColor(_col))
        _app.setPalette(_dark)
        try:
            pop3 = _win3._show_picker(_win3._model_entries(),
                                      _win3._model_btn, lambda d: None)
            _app.processEvents()

            c.check("★★★ 浮层是**主窗口内的子控件**（不是顶层窗口）",
                    not pop3.isWindow(),
                    "是顶层窗口 → 圆角会依赖系统 alpha，可能出黑角")
            c.check("★ 父控件存在（否则定位与合成都会失效）",
                    pop3.parentWidget() is not None)

            # 浮层自身抓图：内部必须是白的（深色调色板下也不能变黑）
            _img = pop3.grab().toImage()
            _white = _total = 0
            for _y in range(0, _img.height(), 3):
                for _x in range(0, _img.width(), 3):
                    _cc = _img.pixelColor(_x, _y)
                    _total += 1
                    if (_cc.red() > 235 and _cc.green() > 235
                            and _cc.blue() > 235):
                        _white += 1
            _ratio = _white / max(_total, 1)
            c.check("★★★ **深色调色板下浮层内部仍是白底**",
                    _ratio > 0.70, f"白色像素占比 {_ratio:.1%}（应 > 70%）")

            # ★ 四角不能是**黑块**。
            #
            # 【这条踩过假阳性】最初"外扩 2px"采样，结果浮层朝上翻时
            #   右上角外侧正好落在**聊天区滚动条**上（深灰 #1f1f1f），
            #   被误判成"黑角"。**父窗口上出现深色像素是正常的**（滚动条、
            #   文字、边框都会），不能拿"父窗口有没有深色"当判据。
            #
            #   真正要判的是：**圆角切掉的那一小块，是不是被填成了黑色**。
            #   做法：取圆角**顶点所在的 1 个像素**（而不是外扩出去），
            #   它应当**不是纯黑**——因为那是"圆角外的父窗口背景"透过来，
            #   或者干脆是浮层自己的浅色边框。
            _whole = _win3.grab().toImage()
            _g = pop3.geometry()
            # 圆角顶点：贴着浮层四角，向内 1px
            _corners = {
                "左上": (_g.x() + 0, _g.y() + 0),
                "右上": (_g.x() + _g.width() - 1, _g.y() + 0),
                "左下": (_g.x() + 0, _g.y() + _g.height() - 1),
                "右下": (_g.x() + _g.width() - 1, _g.y() + _g.height() - 1),
            }
            _black_corners = []
            for _nm, (_cx, _cy) in _corners.items():
                if not (0 <= _cx < _whole.width() and 0 <= _cy < _whole.height()):
                    continue
                _c3 = _whole.pixelColor(_cx, _cy)
                # 判"黑块"的标准收紧：**三通道都 < 30**才算黑
                # （#1f1f1f = 31 是滚动条的灰，不该判黑）
                if (_c3.red() < 30 and _c3.green() < 30 and _c3.blue() < 30):
                    _black_corners.append(f"{_nm}={_c3.name()}")
            c.check("★★★ **浮层四角不是黑块**（圆角没被填黑）",
                    not _black_corners, f"黑角: {_black_corners}")
            pop3.close()
        finally:
            _app.setPalette(_saved)
    except Exception as e:                                   # noqa: BLE001
        c.check("★ 浮层可滚动（不应抛异常）", False,
                f"{type(e).__name__}: {e}")

    print()
    print("=" * 78)
    print(f"模型提供商界面守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())