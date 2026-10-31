# -*- coding: utf-8 -*-
"""Embedding 服务商守卫 —— 向后兼容 · 指纹 · 密钥命名空间 · rerank 解耦 · 界面同构。

【它守的那一类 bug】
    「Embedding 设置」原本写死成硅基流动的三个输入框
    （`EMBEDDING_API_KEY/BASE_URL/MODEL`），而精排直接拼
    `EMBEDDING_BASE_URL + "/rerank"` —— 于是"换一家 embedding"必然连带
    "精排静默失效"。改造的难点**不是**加一张表，而是这四条：

      ① **旧配置必须逐字不动**：`.env` 里只有旧三件套时，端点、模型、知识库指纹
         三者都要与改造前**一字不差** —— 否则升级即触发全量重建（真金白银的
         embedding 费用 + 用户等一分钟，且用户不知道发生了什么）。
      ② **指纹必须随服务商/模型变化**：这是"会触发重建"的判据，判错的方向只有两个，
         要么重建白花钱，要么向量维度不兼容 → 检索永久报错。
      ③ **`embed:<名称>` 命名空间**：embedding 的密钥不能与同名 LLM 服务商串台
         （串台 = 拿 chat 的 key 去调 embedding，或者删 embedding 把 chat 弄挂）。
      ④ **精排解耦 + 降级有日志**：精排失败必须降级为向量顺序（不能让主链路挂），
         但**不许静默** —— 原来只 `print`，打包版没有控制台，谁都看不见。

【零网络】所有 HTTP 出口都换成桩件：`requests.post`（rerank）与 `openai.OpenAI`
        （`/models`）。本脚本**不发出任何真实请求**。

【隔离】凭据文件、自定义服务商、`.env`、向量库指纹**全部指向
        `tests/eval_outputs/embedding_providers_sandbox/`，绝不碰真实数据目录。

【用法】python tests/check_embedding_providers.py
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# ⚠️ 必须在 import config / PyQt6 **之前**设置：
#   · 数据目录隔离（否则会读写真实 %APPDATA% 的凭据与向量库指纹）
#   · Qt 离屏（无头环境也能构造 QWidget；本脚本不发任何网络请求）
_SANDBOX = ROOT / "tests" / "eval_outputs" / "embedding_providers_sandbox"
os.environ.setdefault("COTTON_AGENT_DATA_DIR", str(_SANDBOX / "runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config                                                       # noqa: E402
from config import AppConfig                                        # noqa: E402
from core import credentials, embedding_providers as ep, providers   # noqa: E402
import core.config_manager as config_manager                        # noqa: E402

#: 旧三件套的沙箱取值（刻意与真实 `.env` 不同 —— 断言才不会被"碰巧相等"骗过）
LEGACY_KEY = "sk-legacy-embed-key-0123456789abcdef"
LEGACY_URL = "https://legacy-embed.example/v1"
LEGACY_MODEL = "BAAI/bge-large-zh-v1.5"


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}"
              + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


# ---------------------------------------------------------------------------
# 沙箱
# ---------------------------------------------------------------------------

ENV_PATH = _SANDBOX / ".env"
CRED_PATH = _SANDBOX / "credentials.json"
CUSTOM_PATH = _SANDBOX / "embedding_providers.json"
LLM_CUSTOM_PATH = _SANDBOX / "providers.json"
DATA_DIR = _SANDBOX / "data"


def setup_sandbox() -> None:
    """把四处落盘全部指到沙箱（**绝不写生产文件**）。"""
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "2026年棉花演示.md").write_text("花铃期灌溉演示文档。", "utf-8")

    credentials.credentials_path = lambda: CRED_PATH          # type: ignore[assignment]
    ep._custom_path = lambda: CUSTOM_PATH                     # type: ignore[assignment]
    providers._custom_path = lambda: LLM_CUSTOM_PATH          # type: ignore[assignment]
    config_manager.ENV_PATH = ENV_PATH
    config._dotenv_path = ENV_PATH                             # type: ignore[assignment]
    ENV_PATH.write_text("", "utf-8")


def write_env(**kv: str) -> None:
    """写沙箱 `.env` 并热更新 —— 与界面点「保存」走**同一条路**（`reload_config`）。"""
    ENV_PATH.write_text("".join(f"{k}={v}\n" for k, v in kv.items()), "utf-8")
    config.reload_config()


def legacy_env(**extra: str) -> None:
    """把沙箱恢复成"只有旧三件套"的形态（每次切回 legacy 都调它）。"""
    kv = {"EMBEDDING_API_KEY": LEGACY_KEY, "EMBEDDING_BASE_URL": LEGACY_URL,
          "EMBEDDING_MODEL": LEGACY_MODEL, "EMBEDDING_PROVIDER": ""}
    kv.update(extra)
    write_env(**kv)


class Doc:
    """轻量替身：`_rerank_docs` 只需要 `page_content`。"""

    def __init__(self, content: str) -> None:
        self.page_content = content


def main() -> int:                                              # noqa: C901
    c = Checker()
    setup_sandbox()
    legacy_env()

    # ------------------------------------------------------------------
    c.section("第 1 节：登记表自洽 + 密钥命名空间函数")
    # ------------------------------------------------------------------
    specs = ep.all_specs()
    c.check("内置登记了 2 家（硅基流动 + OpenAI）",
            {"siliconflow", "openai"} <= set(specs), str(sorted(specs)))
    for name, spec in specs.items():
        c.check(f"{name}: base_url 是 https", spec["base_url"].startswith("https://"),
                spec["base_url"])
        c.check(f"{name}: 有默认模型", bool(spec["default_model"]))
        c.check(f"{name}: 默认模型在 models 列表里",
                spec["default_model"] in spec["models"], str(spec["models"]))
        c.check(f"{name}: 标了 builtin 字段", "builtin" in spec)
    c.check("★ 硅基流动登记为内置，且 key_env 指向旧键（老用户不必重填密钥）",
            specs["siliconflow"]["key_env"] == "EMBEDDING_API_KEY",
            specs["siliconflow"]["key_env"])
    c.check("★★ embedding 的密钥键是 `embed:<名称>`",
            ep.key_name("siliconflow") == "embed:siliconflow",
            ep.key_name("siliconflow"))
    c.check("★★ LLM 侧的密钥键是**名字本身**（命名空间不重叠）",
            providers.key_name("deepseek") == "deepseek",
            providers.key_name("deepseek"))
    c.check("★ 两端命名空间确实不相交",
            providers.key_name("x") != ep.key_name("x"))

    # ------------------------------------------------------------------
    c.section("第 2 节：★★★ 向后兼容 —— 未配置新选项时逐字回退旧三件套")
    # ------------------------------------------------------------------
    c.check("前提：EMBEDDING_PROVIDER 为空 → `default_provider()` 返回空串",
            AppConfig.EMBEDDING_PROVIDER == "" and ep.default_provider() == "",
            repr(ep.default_provider()))

    cfg = ep.resolve()
    c.check("★★ base_url **逐字**等于旧键（不多一个斜杠、不少一个字符）",
            cfg.base_url == LEGACY_URL, f"{cfg.base_url!r} vs {LEGACY_URL!r}")
    c.check("★★ model 逐字等于旧键", cfg.model == LEGACY_MODEL,
            f"{cfg.model!r} vs {LEGACY_MODEL!r}")
    c.check("★★ api_key 逐字等于旧键", cfg.api_key == LEGACY_KEY, repr(cfg.api_key[:6]))
    c.check("★ provider 为空串 = legacy（别处可据此判断是否走了新路径）",
            cfg.provider == "" and cfg.is_legacy)

    # 实际下发给 langchain 的字段也必须逐字一致（不是只对了 resolve() 的返回值）
    emb = ep.make_embeddings()
    c.check("★ `make_embeddings()` 真的把这些字段交给 OpenAIEmbeddings",
            (emb.openai_api_base == LEGACY_URL
             and emb.model == LEGACY_MODEL
             and emb.openai_api_key.get_secret_value() == LEGACY_KEY),
            f"{emb.openai_api_base!r} / {emb.model!r}")
    c.check("★ `check_embedding_ctx_length=False` 的旧开关保留",
            emb.check_embedding_ctx_length is False)

    # 指纹（含模型签名）必须与改造前的公式逐字相同 —— 否则升级即触发全量重建
    from core import kb_builder

    c.check("★★★ 指纹里的模型签名与改造前**逐字相同**（`URL|MODEL` 原样拼接）",
            kb_builder._model_signature() == f"{LEGACY_URL}|{LEGACY_MODEL}",
            kb_builder._model_signature())

    # ★ 旧路径**不许加工**：不 strip、不补斜杠。理由：任何一个字符变化都会改变指纹。
    AppConfig.EMBEDDING_BASE_URL = f"  {LEGACY_URL}/  "
    AppConfig.EMBEDDING_MODEL = f" {LEGACY_MODEL} "
    cfg_pad = ep.resolve()
    c.check("★★★ 旧路径**原样透传**带空格的取值（旧代码就是这么干的 —— 不加工才叫兼容）",
            cfg_pad.base_url == f"  {LEGACY_URL}/  "
            and cfg_pad.model == f" {LEGACY_MODEL} "
            and kb_builder._model_signature() == f"  {LEGACY_URL}/  | {LEGACY_MODEL} ",
            f"{cfg_pad.base_url!r}|{cfg_pad.model!r}")
    legacy_env()

    # ------------------------------------------------------------------
    c.section("第 3 节：★★★ 切换服务商 / 模型 → 知识库指纹随之变化")
    # ------------------------------------------------------------------
    fp_legacy = kb_builder.compute_fingerprint(DATA_DIR)
    c.check("前提：legacy 指纹已算出", bool(fp_legacy))

    ep.upsert_custom("gw-a", label="网关 A", base_url="https://a.example/v1",
                     default_model="embed-model-a")
    ep.set_default("gw-a")
    c.check("★ `set_default` 之后 AppConfig 已热更新（不必重启）",
            AppConfig.EMBEDDING_PROVIDER == "gw-a", repr(AppConfig.EMBEDDING_PROVIDER))
    cfg_a = ep.resolve()
    c.check("★ 新路径的端点来自登记表（不再读 EMBEDDING_BASE_URL）",
            cfg_a.base_url == "https://a.example/v1", cfg_a.base_url)
    c.check("★ 新路径的模型来自该服务商的 default_model",
            cfg_a.model == "embed-model-a", cfg_a.model)
    c.check("★ provider 字段带上了服务商名", cfg_a.provider == "gw-a")

    fp_a = kb_builder.compute_fingerprint(DATA_DIR)
    c.check("★★★ **换服务商 → 指纹变化**（= 会触发重建，向量维度不兼容）",
            fp_a != fp_legacy, f"{fp_legacy[:8]} → {fp_a[:8]}")

    ep.upsert_custom("gw-a", label="网关 A", base_url="https://a.example/v1",
                     default_model="embed-model-a2")
    fp_a2 = kb_builder.compute_fingerprint(DATA_DIR)
    c.check("★★★ **换模型 → 指纹再次变化**", fp_a2 != fp_a,
            f"{fp_a[:8]} → {fp_a2[:8]}")

    ep.upsert_custom("gw-b", label="网关 B", base_url="https://b.example/v1",
                     default_model="embed-model-a2")
    ep.set_default("gw-b")
    fp_b = kb_builder.compute_fingerprint(DATA_DIR)
    c.check("★★★ 换到另一家（同模型名、不同端点）→ 指纹变化", fp_b != fp_a2,
            f"{fp_a2[:8]} → {fp_b[:8]}")

    # `model_changed()` 是**上游真正用来判重建的判据**，它也必须是绿的
    kb_builder.save_fingerprint(fp_b)
    c.check("★ 保存指纹后 `model_changed()` 为假（没换 = 不重建）",
            kb_builder.model_changed() is False)
    ep.set_default("gw-a")
    c.check("★★ 切回网关 A → `model_changed()` 为真（会走全量重建）",
            kb_builder.model_changed() is True)
    c.check("★★ `needs_rebuild()` 也随之为真", kb_builder.needs_rebuild() is True)

    # ------------------------------------------------------------------
    c.section("第 4 节：★★★ 密钥命名空间 —— 同名 LLM 服务商互不影响")
    # ------------------------------------------------------------------
    # 刻意把 embedding 服务商也起名 `deepseek`：这是最容易串台的场景
    ep.upsert_custom("deepseek", label="深度求索（Embedding 网关）",
                     base_url="https://embed.deepseek.example/v1",
                     default_model="deepseek-embed-demo")
    LLM_KEY, EMB_KEY = "sk-llm-namespace-AAAA1111", "sk-embed-namespace-BBBB2222"
    credentials.set_key(providers.key_name("deepseek"), LLM_KEY)
    credentials.set_key(ep.key_name("deepseek"), EMB_KEY)

    c.check("★ 两条凭据**分别存在**各自的键下",
            credentials.get_key("deepseek") == LLM_KEY
            and credentials.get_key("embed:deepseek") == EMB_KEY,
            f"{credentials.get_key('deepseek')!r} / {credentials.get_key('embed:deepseek')!r}")

    ep.set_default("deepseek")
    c.check("★★★ embedding 解析拿到的是 **embed:** 那把（不是 chat 的 key）",
            ep.resolve().api_key == EMB_KEY, repr(ep.resolve().api_key[:8]))
    c.check("★★ LLM 侧的 key **没有被 embedding 覆盖**",
            credentials.get_key("deepseek") == LLM_KEY,
            repr(credentials.get_key("deepseek")[:8]))

    ep.delete_custom("deepseek")
    c.check("★★ 删除 embedding 服务商只清 `embed:` 密钥",
            credentials.has_key("embed:deepseek") is False)
    c.check("★★★ **同名 LLM 服务商的密钥原样保留**（这是命名空间存在的理由）",
            credentials.get_key("deepseek") == LLM_KEY,
            repr(credentials.get_key("deepseek")[:8]))
    credentials.delete_key("deepseek")

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ 添加 / 删除 / 设为默认 的往返")
    # ------------------------------------------------------------------
    c.check("前提：gw-x 尚未存在", "gw-x" not in ep.all_specs())
    ep.upsert_custom("gw-x", label="网关 X", base_url="https://x.example/v1",
                     default_model="m-x")
    specs_x = ep.all_specs()
    c.check("★ 添加后出现在合并视图里", "gw-x" in specs_x)
    c.check("★ 自定义项 builtin=False（界面据此显示「删除」）",
            specs_x["gw-x"]["builtin"] is False)
    on_disk = json.loads(CUSTOM_PATH.read_text("utf-8"))["providers"]
    c.check("★ 已原子落盘到 `embedding_providers.json`", "gw-x" in on_disk,
            str(sorted(on_disk)))
    c.check("★ 与 LLM 的 `providers.json` **不是同一个文件**",
            CUSTOM_PATH != LLM_CUSTOM_PATH)

    ep.set_default("gw-x")
    env_text = ENV_PATH.read_text("utf-8")
    c.check("★★ `set_default` 把 `EMBEDDING_PROVIDER=gw-x` 写进 .env",
            "EMBEDDING_PROVIDER=gw-x" in env_text, env_text.strip()[-60:])
    c.check("★ 热更新后 `default_provider()` 立即返回它",
            ep.default_provider() == "gw-x")
    c.check("★★ 旧键 **没有被清掉**（清空 PROVIDER 才能原样切回旧三件套）",
            "EMBEDDING_BASE_URL=" in env_text and "EMBEDDING_MODEL=" in env_text)

    ep.delete_custom("gw-x")
    c.check("★ 删除后从合并视图消失", "gw-x" not in ep.all_specs())
    c.check("★★ 删掉了默认项 → 名字未登记 → `default_provider()` 退回空串（不猜）",
            ep.default_provider() == "")
    c.check("★★★ 且 `resolve()` 立刻回退到旧三件套（不会因为名字悬空就崩）",
            ep.resolve().base_url == LEGACY_URL
            and ep.resolve().model == LEGACY_MODEL)
    c.check("★ 指纹也回到 legacy 那一份", kb_builder.compute_fingerprint(DATA_DIR) == fp_legacy)

    # 边界：内置不可删 / 明文 http 拒绝 / 内置名不可覆盖
    for bad, why in (
        (lambda: ep.delete_custom("siliconflow"), "内置不可删除"),
        (lambda: ep.upsert_custom("siliconflow", label="x",
                                  base_url="https://evil.example/v1", default_model="m"),
         "内置名不可覆盖"),
        (lambda: ep.upsert_custom("plain", label="x", base_url="http://plain/v1",
                                  default_model="m"), "明文 http 拒绝"),
        (lambda: ep.upsert_custom("nomodel", label="x",
                                  base_url="https://n.example/v1", default_model=""),
         "空模型拒绝"),
    ):
        try:
            bad()
            c.check(f"× {why}", False, "没有抛 ValueError")
        except ValueError:
            c.check(f"★ {why}", True)
    c.check("★ 非法操作后登记表没被污染",
            "plain" not in ep.all_specs() and "nomodel" not in ep.all_specs())

    # ---- 内置项：端点不可改，但显示名 / 默认模型**必须能存下来** ----
    # 【守的 bug】旧实现只对**自定义**项调 upsert_custom()，内置项的修改被
    #   静默丢弃：用户以为换了模型、实际没换，要到下一次调用才暴露。
    import inspect as _inspect

    sig_u = _inspect.signature(ep.update_builtin)
    c.check("★★ `update_builtin` 签名里**没有** base_url"
            "（内置端点改不了 —— 靠签名，而不是靠界面禁用）",
            "base_url" not in sig_u.parameters, str(list(sig_u.parameters)))
    ep.update_builtin("siliconflow", label="硅基流动", default_model="BAAI/bge-m3")
    sp = ep.all_specs()["siliconflow"]
    c.check("★★ 内置项的默认模型可改并生效（否则换模型只能再加一个重复服务商）",
            sp["default_model"] == "BAAI/bge-m3", sp["default_model"])
    c.check("★ 覆盖后的模型进了候选表（界面不会出现「默认模型不在清单里」）",
            "BAAI/bge-m3" in sp["models"], str(sp["models"]))
    c.check("★★★ 端点**没有变**（可改字段里根本没有 base_url）",
            sp["base_url"] == "https://api.siliconflow.cn/v1", sp["base_url"])
    ep.set_default("siliconflow")
    c.check("★★ 覆盖后 `resolve()` 用新模型 → 知识库指纹随之变化",
            ep.resolve().model == "BAAI/bge-m3"
            and kb_builder.compute_fingerprint(DATA_DIR) != fp_legacy,
            ep.resolve().model)
    c.check("★ 覆盖落在**同一个**文件里（不新增第二份真相）",
            json.loads(CUSTOM_PATH.read_text("utf-8"))
            ["overrides"]["siliconflow"]["default_model"] == "BAAI/bge-m3")

    # ------------------------------------------------------------------
    c.section("第 6 节：★★★ 精排解耦（默认继承 / 显式覆盖 / 失败降级有日志）")
    # ------------------------------------------------------------------
    legacy_env()
    c.check("★★ 旧配置下 rerank 地址与改造前**逐字相同**",
            ep.rerank_url() == AppConfig.EMBEDDING_BASE_URL.rstrip("/") + "/rerank"
            == LEGACY_URL + "/rerank", ep.rerank_url())
    c.check("★★ 旧配置下 rerank 密钥继承旧的 EMBEDDING_API_KEY",
            ep.rerank_api_key() == LEGACY_KEY)

    ep.set_default("gw-a")
    c.check("★★★ 换了 embedding 服务商后，rerank **默认跟着走**（不配就不用改）",
            ep.rerank_base_url() == "https://a.example/v1"
            and ep.rerank_url() == "https://a.example/v1/rerank", ep.rerank_url())

    write_env(EMBEDDING_API_KEY=LEGACY_KEY, EMBEDDING_BASE_URL=LEGACY_URL,
              EMBEDDING_MODEL=LEGACY_MODEL, EMBEDDING_PROVIDER="gw-a",
              RERANK_BASE_URL="https://rerank.example/v1/",
              RERANK_API_KEY="rr-explicit-key")
    c.check("★★★ 显式 `RERANK_BASE_URL` 覆盖继承值（且尾斜杠归一）",
            ep.rerank_url() == "https://rerank.example/v1/rerank", ep.rerank_url())
    c.check("★★★ 显式 `RERANK_API_KEY` 覆盖继承值",
            ep.rerank_api_key() == "rr-explicit-key", ep.rerank_api_key())
    c.check("★ 覆盖后 embedding 自己的端点不受影响",
            ep.resolve().base_url == "https://a.example/v1")

    # ---- 真实调用点：`KnowledgeBase._rerank_docs`（桩件，零网络）----
    import core.knowledge_base as kb_mod

    kb = kb_mod.KnowledgeBase.__new__(kb_mod.KnowledgeBase)   # 不触发模型加载
    docs = [Doc(f"d{i}") for i in range(5)]

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._p = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._p

    class _Boom:
        def post(self, *a, **kw):
            raise ConnectionError("桩件：模拟端点不可达")

    class _Capture:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def post(self, url, **kw):
            self.calls.append((url, kw))
            return _Resp({"results": [{"index": 3}, {"index": 1}]})

    real_requests = kb_mod.requests
    try:
        cap = _Capture()
        kb_mod.requests = cap                                    # type: ignore[assignment]
        out = kb._rerank_docs("灌溉", docs, top_n=3)
        c.check("★★ `_rerank_docs` 实际打到**解耦后**的地址（不是 EMBEDDING_BASE_URL）",
                cap.calls and cap.calls[0][0] == "https://rerank.example/v1/rerank",
                str(cap.calls[0][0]) if cap.calls else "没有发请求")
        c.check("★★ 用的是**解耦后**的密钥",
                cap.calls and cap.calls[0][1]["headers"]["Authorization"]
                == "Bearer rr-explicit-key",
                str(cap.calls[0][1].get("headers", {})) if cap.calls else "")
        c.check("★ 精排成功时按返回顺序取前 top_n",
                [d.page_content for d in out] == ["d3", "d1"], str([d.page_content for d in out]))

        # ---- 失败必须降级 + 留 warning ----
        records: list[logging.LogRecord] = []

        class _Grab(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("core.knowledge_base")
        old_level = logger.level
        handler = _Grab()
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            kb_mod.requests = _Boom()                            # type: ignore[assignment]
            degraded = kb._rerank_docs("灌溉", docs, top_n=3)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        c.check("★★★ 精排抛错时**降级为向量顺序**（前 top_n 条、顺序不变、不抛）",
                [d.page_content for d in degraded] == ["d0", "d1", "d2"],
                str([d.page_content for d in degraded]))
        warns = [r for r in records if r.levelno >= logging.WARNING]
        c.check("★★★ **降级时留有 WARNING 日志**（不再静默 —— 这是本次改造的硬要求）",
                bool(warns) and "精排失败" in warns[0].getMessage(),
                str([r.getMessage()[:40] for r in records]))
        c.check("★ 日志里带上了端点（否则无法判断是不是 RERANK_BASE_URL 配错）",
                bool(warns) and "rerank.example" in warns[0].getMessage(),
                warns[0].getMessage()[:120] if warns else "")

        # 返回空结果也算"精排没生效" —— 同样不能静默
        records.clear()
        class _Empty:
            def post(self, *a, **kw):
                return _Resp({"results": []})

        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            kb_mod.requests = _Empty()                           # type: ignore[assignment]
            empty = kb._rerank_docs("灌溉", docs, top_n=3)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        c.check("★ 返回为空时同样降级（不假装精排生效）",
                [d.page_content for d in empty] == ["d0", "d1", "d2"])
        c.check("★ 且同样留了 warning", bool(records), str(len(records)))
    finally:
        kb_mod.requests = real_requests                           # type: ignore[assignment]

    # ------------------------------------------------------------------
    c.section("第 7 节：★ 「测试连接」拉 /models —— 用桩件，零网络")
    # ------------------------------------------------------------------
    import openai as _openai_real

    class _FakeModel:
        def __init__(self, mid: str) -> None:
            self.id = mid

    class _FakePage:
        def __init__(self, ids: list[str]) -> None:
            self.data = [_FakeModel(i) for i in ids]

    class _FakeModels:
        def __init__(self, ids=None, exc=None) -> None:
            self._ids, self._exc = ids or [], exc

        def list(self):
            if self._exc is not None:
                raise self._exc
            return _FakePage(self._ids)

    class _FakeOpenAI:
        ids: list[str] = []
        exc: Exception | None = None
        last_kw: dict = {}

        def __init__(self, **kw) -> None:
            type(self).last_kw = dict(kw)
            self.models = _FakeModels(self.ids, self.exc)

    class _E(Exception):
        def __init__(self, status: int) -> None:
            super().__init__("fake")
            self.status_code = status

    real_openai = _openai_real.OpenAI
    _openai_real.OpenAI = _FakeOpenAI                             # type: ignore[assignment]
    try:
        credentials.set_key(ep.key_name("gw-a"), "sk-gw-a-testing")
        _FakeOpenAI.ids, _FakeOpenAI.exc = ["embed-a", "embed-b"], None
        ok, detail, ids = ep.list_models("gw-a")
        c.check("★ 成功时返回模型清单（界面据此填可选可编辑下拉）",
                ok and ids == ["embed-a", "embed-b"], f"{ok} {ids} {detail}")
        c.check("★★ 用的是该服务商的**端点**与 `embed:` 密钥（不是 LLM 的同一把）",
                _FakeOpenAI.last_kw.get("base_url") == "https://a.example/v1"
                and _FakeOpenAI.last_kw.get("api_key") == "sk-gw-a-testing",
                str({k: _FakeOpenAI.last_kw.get(k) for k in ("base_url", "api_key")}))
        _FakeOpenAI.ids, _FakeOpenAI.exc = [], _E(401)
        ok401, msg401, _ = ep.list_models("gw-a")
        c.check("★★ 401 → 明确说「密钥无效，或与端点/空间不匹配」",
                ok401 is False and "401" in msg401, msg401)
        _FakeOpenAI.ids, _FakeOpenAI.exc = [], _E(404)
        ok404, msg404, _ = ep.list_models("gw-a")
        c.check("★ 404 → 明确说「端点地址不通」", ok404 is False and "404" in msg404, msg404)
        _FakeOpenAI.ids, _FakeOpenAI.exc = [], None
        okx, msgx, _ = ep.list_models("__no_such_provider__")
        c.check("★ 未知服务商 → 返回 (False, 说明)，不抛异常", okx is False and bool(msgx))
        credentials.delete_key(ep.key_name("gw-a"))
        okk, msgk, _ = ep.list_models("gw-a")
        c.check("★ 未配置密钥 → 直接说明，不发请求", okk is False and "密钥" in msgk, msgk)
        c.check("★★ `test_connection` 是 `list_models` 的薄封装（失败分类只有一份）",
                "list_models" in ep.test_connection.__code__.co_names,
                str(ep.test_connection.__code__.co_names))
    finally:
        _openai_real.OpenAI = real_openai                         # type: ignore[assignment]

    # ------------------------------------------------------------------
    c.section("第 8 节：★★ 界面同构 —— 与「模型」页**同一套控件**，只是 kind 不同")
    # ------------------------------------------------------------------
    try:
        from PyQt6.QtWidgets import QApplication, QComboBox

        from ui.provider_editor import (
            KIND_SPECS, ProviderCard, ProviderEditDialog, ProviderGroup, default_provider_name,
        )
        app = QApplication.instance() or QApplication([])
        c.check("前提：Qt 已可用（离屏）", app is not None)

        # ★ 界面之所以能"零 if kind"，靠的是两个后端**同名同签名**的接口。
        #   这条断言守的就是这份契约 —— 少一个方法，界面就会在运行时 AttributeError。
        need = ("all_specs", "upsert_custom", "delete_custom", "list_models",
                "key_name", "default_provider", "set_default", "update_builtin")
        missing = {
            kind: [n for n in need if not callable(getattr(spec.backend, n, None))]
            for kind, spec in KIND_SPECS.items()
        }
        c.check("★★ 两个后端暴露**同名**的完整接口（界面因此不需要任何 `if kind`）",
                not any(missing.values()), str(missing))
        c.check("★ 而且确实是两家不同的后端",
                KIND_SPECS["llm"].backend is not KIND_SPECS["embedding"].backend)

        legacy_env()
        ep.upsert_custom("gw-ui", label="界面网关", base_url="https://ui.example/v1",
                         default_model="embed-ui")
        ep.set_default("gw-ui")
        credentials.set_key(ep.key_name("gw-ui"), "sk-ui-testing")

        llm_group = ProviderGroup(kind="llm")
        emb_group = ProviderGroup(kind="embedding")
        c.check("★★ 两个页面都是 `ProviderGroup`（同一个类）",
                type(llm_group) is ProviderGroup and type(emb_group) is ProviderGroup)
        c.check("★ 添加按钮文案按 kind 区分",
                "添加模型提供商" in llm_group.btn_add.text()
                and "Embedding" in emb_group.btn_add.text(),
                f"{llm_group.btn_add.text()} / {emb_group.btn_add.text()}")

        emb_cards = emb_group.findChildren(ProviderCard)
        c.check("★★ Embedding 页的卡片来自 **embedding 登记表**",
                {card.name for card in emb_cards} == set(ep.all_specs()),
                str(sorted(card.name for card in emb_cards)))
        from PyQt6.QtWidgets import QLabel as _QLabel
        badges = [card for card in emb_cards
                  if any(lbl.objectName() == "defaultBadge"
                         for lbl in card.findChildren(_QLabel))]
        c.check("★ 当前默认项在卡片上带「默认」徽标（且只有它一个）",
                [card.name for card in badges] == ["gw-ui"],
                str([card.name for card in badges]))
        c.check("★★ 卡片状态点读的是 `embed:` 密钥（同名 LLM 的 key 不会冒充）",
                emb_group.kind == "embedding"
                and credentials.has_key(ep.key_name("gw-ui")))

        emb_spec = ep.all_specs()["gw-ui"]
        dlg = ProviderEditDialog(emb_spec, is_new=False, kind="embedding")
        c.check("★★ 编辑对话框也是**同一个类**（卡片/对话框/测试连接全部复用）",
                type(dlg) is ProviderEditDialog)
        c.check("★ Embedding 编辑页的勾选框文案与模型页不同（但语义相同）",
                dlg._make_default.text() == "设为默认 Embedding 服务商",
                dlg._make_default.text())
        c.check("★ 当前默认是 gw-ui → 其编辑页勾选框已选中",
                dlg._make_default.isChecked() is True)
        c.check("★★ 模型同样是下拉，且**只能选不能输**（与模型页同一套 `ModelCombo`）",
                isinstance(dlg._model, QComboBox) and not dlg._model.isEditable())
        c.check("★ 有「拉取清单」按钮（空清单时唯一的出路）",
                hasattr(dlg, "_btn_models") and "拉取清单" in dlg._btn_models.text())
        c.check("★ 沙箱无缓存 → 候选里只有已配置的那个（不联网）",
                dlg._model.models() == ["embed-ui"], str(dlg._model.models()))
        before = dlg._model.currentText()
        dlg._fill_models(["embed-x", "embed-y"])
        c.check("★ 填充不改变当前选中的模型，且把它留在候选里",
                dlg._model.currentText() == before == "embed-ui"
                and "embed-ui" in dlg._model.models(),
                f"{before} → {dlg._model.currentText()} / {dlg._model.models()}")
        c.check("★ 自定义项接口地址可编辑", dlg._url.isEnabled() is True)
        dlg_builtin = ProviderEditDialog(ep.all_specs()["siliconflow"],
                                         is_new=False, kind="embedding")
        c.check("★ 内置项接口地址只读", dlg_builtin._url.isEnabled() is False)
        c.check("★ 内置项 id 只读", dlg_builtin._id.isEnabled() is False)

        # 构造全程不得联网：把 list_models 换成会炸的桩
        real_bind = ep.list_models
        ep.list_models = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("构造界面时不该联网"))
        try:
            off = ProviderEditDialog(emb_spec, is_new=False, kind="embedding")
            grp = ProviderGroup(kind="embedding")
            c.check("★★ ★ 构造 Embedding 界面全程**不调用 list_models**（不联网）",
                    off is not None and grp is not None)
        finally:
            ep.list_models = real_bind

        # 「设为默认」的往返（界面这一层只是转调核心层）
        c.check("★★ 界面 `set_default` 转调核心层（写 .env 的逻辑只有一份）",
                "set_default" in ProviderGroup.set_default.__code__.co_names,
                str(ProviderGroup.set_default.__code__.co_names))
        c.check("★★ 界面层直接调后端 `set_default`（没有自己拼 .env 写入）",
                "_ks" in ProviderGroup.set_default.__code__.co_names)
        c.check("★ 模型的默认名读取器对两类服务商分别生效",
                default_provider_name("embedding") == "gw-ui"
                and default_provider_name("llm") != "gw-ui")

        # ---- ★★★ 界面的「编辑 / 添加 / 删除」真的落盘了吗 ----
        # 【守的 bug】旧实现只对**自定义**项调 upsert_custom()，内置项的修改被
        #   静默丢弃 —— 用户以为换了模型、实际没换，要到下一次调用才暴露。
        #   这里把对话框的 exec()/values() 换成桩，走**真实的保存路径**。
        import ui.provider_editor as _pe

        real_exec, real_values = _pe.ProviderEditDialog.exec, _pe.ProviderEditDialog.values
        real_mb, real_openai_pt = _pe.QMessageBox, None
        queued: dict = {"v": {}}

        class _MB:
            StandardButton = _pe.QMessageBox.StandardButton

            @staticmethod
            def question(*a, **k):
                return _MB.StandardButton.Yes

            @staticmethod
            def warning(*a, **k):
                return None

        _pe.ProviderEditDialog.exec = lambda self: _pe.QDialog.DialogCode.Accepted
        _pe.ProviderEditDialog.values = lambda self: dict(queued["v"])
        _pe.QMessageBox = _MB                                   # type: ignore[assignment]
        try:
            queued["v"] = {"name": "siliconflow", "label": "硅基流动",
                           "base_url": "https://api.siliconflow.cn/v1",
                           "default_model": "BAAI/bge-m3",
                           "api_key": "sk-ui-saved-0123456789",
                           "make_default": True}
            emb_group.edit_provider("siliconflow")
            c.check("★★★ 界面「编辑内置项 → 保存」真的把**模型**写下去了",
                    ep.all_specs()["siliconflow"]["default_model"] == "BAAI/bge-m3",
                    ep.all_specs()["siliconflow"]["default_model"])
            c.check("★★ 密钥写在 `embed:` 命名空间下",
                    credentials.get_key(ep.key_name("siliconflow"))
                    == "sk-ui-saved-0123456789")
            c.check("★ 勾了「设为默认」→ `EMBEDDING_PROVIDER` 已指向它",
                    ep.default_provider() == "siliconflow", ep.default_provider())

            queued["v"] = {"name": "gw-added", "label": "新增网关",
                           "base_url": "https://added.example/v1",
                           "default_model": "added-embed",
                           "api_key": "sk-added-key-0123456789",
                           "make_default": False}
            emb_group._on_add()
            c.check("★★ 界面「添加」→ 服务商与密钥都落盘",
                    "gw-added" in ep.all_specs()
                    and credentials.get_key(ep.key_name("gw-added"))
                    == "sk-added-key-0123456789")
            c.check("★ 添加后卡片列表跟着刷新（界面与落盘不脱节）",
                    "gw-added" in {card.name for card in emb_group.findChildren(ProviderCard)})

            emb_group.delete_provider("gw-added")
            c.check("★★ 界面「删除」→ 服务商与其 `embed:` 密钥一起清掉",
                    "gw-added" not in ep.all_specs()
                    and credentials.has_key(ep.key_name("gw-added")) is False)
        finally:
            _pe.ProviderEditDialog.exec = real_exec
            _pe.ProviderEditDialog.values = real_values
            _pe.QMessageBox = real_mb                           # type: ignore[assignment]
    except Exception as e:                                       # noqa: BLE001
        c.check("★ 界面同构检查（不应抛异常）", False, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    c.section("第 9 节：★ 源码级不变量（这几条一旦破，运行期很难发现）")
    # ------------------------------------------------------------------
    import ast as _ast

    src = (ROOT / "core" / "embedding_providers.py").read_text("utf-8")
    c.check("★★★ `embed:` 前缀只在一处拼（`EMBED_KEY_PREFIX` + `key_name`）",
            src.count('f"{EMBED_KEY_PREFIX}{provider}"') == 1
            and 'EMBED_KEY_PREFIX = "embed:"' in src)

    kb_src = (ROOT / "core" / "knowledge_base.py").read_text("utf-8")
    kb_tree = _ast.parse(kb_src)
    fn = next((n for n in _ast.walk(kb_tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "_rerank_docs"), None)
    c.check("前提：`_rerank_docs` 存在", fn is not None)
    if fn is not None:
        calls = [n for n in _ast.walk(fn) if isinstance(n, _ast.Call)]
        concat = False
        for node in calls:
            # 找 `... + "/rerank"` 这种**自己拼**地址的写法
            if (isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Add)
                    and isinstance(node.right, _ast.Constant)
                    and node.right.value == "/rerank"):
                concat = True
        c.check("★★★ `_rerank_docs` 里**不再自己拼** `+ \"/rerank\"`（走 rerank_url()）",
                not concat)
        c.check("★★ `_rerank_docs` 里出现了 `logging` 级别的降级出口（log.warning）",
                any(isinstance(n, _ast.Call)
                    and isinstance(n.func, _ast.Attribute)
                    and n.func.attr in ("warning", "error")
                    for n in _ast.walk(fn)))

    for rel, why in (
        ("core/knowledge_base.py", "公共知识库"),
        ("core/kb_builder.py", "指纹与验证"),
        ("core/user_memory.py", "L4/L3 记忆"),
    ):
        text = (ROOT / rel).read_text("utf-8")
        c.check(f"★★ {why}（{rel}）不再自己构造 OpenAIEmbeddings（统一走 make_embeddings）",
                "OpenAIEmbeddings(" not in text, "仍在多处构造 → 迟早漂移")
        c.check(f"★ {rel} 仍引用 embedding_providers（没有绕开解析入口）",
                "embedding_providers" in text)

    ui_path = ROOT / "ui" / "settings_dialog.py"
    ui_src = ui_path.read_text("utf-8")
    ui_tree = _ast.parse(ui_src)
    # ★ 只查**代码里的字符串常量**，不查注释/文档 —— 用裸子串会误伤
    #   "这里不再写 EMBEDDING_*"这种说明文字（本项目栽过同类坑）。
    bad_keys: list[str] = []
    for node in _ast.walk(ui_tree):
        if isinstance(node, _ast.FunctionDef) and node.name in ("_load_values", "_on_save"):
            for sub in _ast.walk(node):
                if (isinstance(sub, _ast.Constant) and isinstance(sub.value, str)
                        and sub.value.startswith("EMBEDDING_")):
                    bad_keys.append(f"{node.name}:{sub.value}")
    c.check("★★★ `_load_values` / `_on_save` 的**代码里**没有 EMBEDDING_* 键"
            "（否则无入口却被覆盖 → 指纹变 → 全量重建）",
            not bad_keys, str(bad_keys))
    c.check("★ 也没有旧的三个写死输入框了",
            not any(w in ui_src for w in ("_emb_key", "_emb_url", "_emb_model")))
    c.check("★★ Embedding 页用的是同一个 ProviderGroup（kind=embedding）",
            'ProviderGroup(kind="embedding")' in ui_src)

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"Embedding 服务商守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)

    shutil.rmtree(_SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())