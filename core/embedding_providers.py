# -*- coding: utf-8 -*-
"""Embedding 服务商登记表 —— 与 `core/providers.py`（LLM）**同构**，但**独立命名空间**。

【为什么另起一张表，而不是塞进 LLM 登记表】
  · 同一个名字在两边是**两家不同的服务**：`deepseek` 的 chat 端点不是 embedding 端点，
    key 也不通用；塞进一张表就得给每个字段再加一个 kind 维度，迟早有人漏判 kind。
  · 两边的"坑"不一样：LLM 关心 tool calling / developer role；Embedding 关心
    **向量维度**（换模型必须重建知识库）和 **rerank 是不是这家专有的接口形状**。

【三条硬规则】
  ① **`EMBEDDING_PROVIDER` 留空 ⇒ 完全走旧三件套**（`EMBEDDING_API_KEY/BASE_URL/MODEL`），
     **逐字不变**：端点、模型、`kb_builder._model_signature()` 三者与改造前一致。
     旧用户不动任何配置，知识库**不会**被无谓重建。见 `resolve()`。
  ② **密钥命名空间 = `embed:<名称>`**（`key_name()` 是唯一拼这个名字的地方）。
     这样即使有人把 embedding 服务商也起名 `deepseek`，它与 LLM 的 `deepseek`
     **是两条互不影响的凭据记录**。守卫 `tests/check_embedding_providers.py` 盯着。
  ③ **精排与 Embedding 解耦**：`RERANK_BASE_URL/RERANK_API_KEY` 默认**继承**
     Embedding 的（不配就不变），显式配置则覆盖（`rerank_url()/rerank_api_key()`）。
     原因：`POST {base_url}/rerank` 是硅基流动**专有**的接口形状，
     换一家 embedding 就未必有这条路 —— 所以精排要能单独指回原来那家。

【持久化】自定义服务商 → `app_data_dir()/embedding_providers.json`
（与 LLM 的 `providers.json` **不同文件**：两边各自增删互不干扰）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config import AppConfig

log = logging.getLogger(__name__)

#: 密钥命名空间前缀 —— ★ **全项目只有 `key_name()` 拼这个字符串**。
EMBED_KEY_PREFIX = "embed:"

#: 自定义服务商落盘文件名（与 LLM 的 providers.json 分开）
CUSTOM_FILE_NAME = "embedding_providers.json"


@dataclass(frozen=True)
class EmbeddingProvider:
    """一家 OpenAI 兼容的 Embedding 服务商。"""

    name: str                       # 内部 id（固定不可改：它是配置与日志的键）
    label: str                      # 界面上显示的名字
    base_url: str
    key_env: str                    # 内置项从哪个环境变量取 key（自定义项为空）
    default_model: str
    models: tuple[str, ...] = ()    # 已知可用模型（供下拉；可被「测试连接」刷新）
    #: 兼容性备注（人读）
    compat: tuple[str, ...] = ()


#: 内置登记表。
#:
#: ★ `siliconflow` 刻意登记为内置项，且 `key_env="EMBEDDING_API_KEY"`：
#:   这是**旧配置的桥** —— 老用户的 `.env` 里只有 `EMBEDDING_API_KEY`，
#:   把它选为默认服务商时不必重新填一次密钥（`credentials._env_key` 会兜到这里）。
BUILTINS: dict[str, EmbeddingProvider] = {
    "siliconflow": EmbeddingProvider(
        name="siliconflow",
        label="硅基流动",
        base_url="https://api.siliconflow.cn/v1",
        key_env="EMBEDDING_API_KEY",
        default_model="BAAI/bge-large-zh-v1.5",
        models=(
            "BAAI/bge-large-zh-v1.5",
            "BAAI/bge-large-en-v1.5",
            "BAAI/bge-m3",
            "netease-youdao/bce-embedding-base_v1",
        ),
        compat=(
            "★ 精排走它家**专有**的 POST /rerank（不是 OpenAI 兼容的一部分）",
            "换模型会改变向量维度 → 知识库必须全量重建",
        ),
    ),
    "openai": EmbeddingProvider(
        name="openai",
        label="OpenAI（官方）",
        base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        default_model="text-embedding-3-small",
        models=(
            "text-embedding-3-small",
            "text-embedding-3-large",
            "text-embedding-ada-002",
        ),
        compat=("没有 /rerank 端点 → 精排请用 RERANK_BASE_URL 指回原服务商",),
    ),
}


# ---------------------------------------------------------------------------
# 密钥命名空间
# ---------------------------------------------------------------------------

def key_name(provider: str) -> str:
    """服务商名 → **凭据层里的键**（`embed:<名称>`）。

    ★ 与 `core/providers.key_name()`（恒等函数）对称：界面只调 `key_name()`，
      于是"embedding 的密钥存在哪个键下"**只有这一处知道**。
    """
    return f"{EMBED_KEY_PREFIX}{provider}"


def is_embed_key(key: str) -> bool:
    """这个凭据键是不是 embedding 命名空间里的。"""
    return str(key or "").startswith(EMBED_KEY_PREFIX)


def env_name_for_key(key: str) -> str:
    """`embed:<名称>` → 该服务商的 `key_env`（自定义项 / 非 embed 键返回空串）。

    供 `core/credentials._env_key()` 使用 —— 否则凭据层只认 LLM 登记表，
    老用户 `.env` 里的 `EMBEDDING_API_KEY` 就"看不见"（卡片显示红点、实际能用）。
    """
    if not is_embed_key(key):
        return ""
    p = BUILTINS.get(str(key)[len(EMBED_KEY_PREFIX):])
    return p.key_env if p is not None else ""


# ---------------------------------------------------------------------------
# 自定义服务商（持久化）
# ---------------------------------------------------------------------------

def _custom_path() -> Path:
    """自定义 embedding 服务商文件（守卫会把它指到沙箱）。"""
    from config import app_data_dir
    return Path(app_data_dir()) / CUSTOM_FILE_NAME


def _load_all() -> dict:
    """读整个文件（`providers` + `overrides`）。**任何异常都降级为空**。"""
    try:
        p = _custom_path()
        if not p.is_file():
            return {}
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def load_custom() -> dict[str, dict]:
    """读自定义服务商。坏文件/坏字段 → 空 dict（不该让应用起不来）。"""
    provs = _load_all().get("providers")
    return provs if isinstance(provs, dict) else {}


def load_overrides() -> dict[str, dict]:
    """读**内置项的可改字段覆盖**（`label` / `default_model`）。

    ★ 为什么需要它：内置项的 `base_url` 不能改（地址里编着"对不对"的信息），
      但"这家用哪个 embedding 模型"必须能改 —— 否则用户为了换个模型就得
      再添加一个与内置项一模一样的自定义服务商，那是纯粹的自找麻烦。
      端点不提供覆盖入口 ⇒ "改坏内置端点"在**参数层面**就做不到。
    """
    got = _load_all().get("overrides")
    return got if isinstance(got, dict) else {}


def _save_all(provs: dict[str, dict], overrides: dict[str, dict]) -> None:
    """原子写（临时文件 + `os.replace`），与项目既有约定一致。"""
    p = _custom_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 2, "providers": provs, "overrides": overrides},
                         ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".embprov_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_custom(provs: dict[str, dict]) -> None:
    """写自定义服务商（**保留已有覆盖**）。"""
    _save_all(provs, load_overrides())


def update_builtin(name: str, *, label: str = "",
                   default_model: str = "") -> None:
    """更新内置项的**可改字段**（显示名 / 默认模型）。

    ⚠️ 签名里**没有 `base_url`** —— 内置端点不是"界面上禁用"，而是
       调用层面就改不了。改错端点会得到"看着像密钥失效"的 401，不值得给这个口子。
    """
    if name not in BUILTINS:
        raise ValueError(f"{name!r} 不是内置服务商（自定义项请用 upsert_custom）")
    if not default_model.strip():
        raise ValueError("默认模型不能为空")
    overrides = load_overrides()
    cur = dict(overrides.get(name) or {})
    cur["label"] = label or BUILTINS[name].label
    cur["default_model"] = default_model.strip()
    overrides[name] = cur
    _save_all(load_custom(), overrides)


def all_specs() -> dict[str, dict]:
    """界面用的**合并视图**：内置（含覆盖） + 自定义。

    每项 `{"name","label","base_url","default_model","models","builtin","key_env","compat"}`。
    自定义项**不覆盖内置项**（同名以内置为准）—— 内置端点里编着"对不对"的信息。
    """
    out: dict[str, dict] = {}
    overrides = load_overrides()
    for name, p in BUILTINS.items():
        ov = overrides.get(name)
        ov = ov if isinstance(ov, dict) else {}
        label = str(ov.get("label") or p.label)
        model = str(ov.get("default_model") or p.default_model)
        models = list(p.models) or [p.default_model]
        # ★ 覆盖的模型必须进候选表 —— 否则界面会"默认模型不在自己的清单里"
        if model not in models:
            models.insert(0, model)
        out[name] = {"name": name, "label": label, "base_url": p.base_url,
                     "default_model": model, "builtin": True,
                     "models": models,
                     "key_env": p.key_env, "compat": list(p.compat)}
    for name, d in load_custom().items():
        if name in out or not isinstance(d, dict):
            continue
        url = str(d.get("base_url") or "")
        model = str(d.get("default_model") or "")
        if not url.startswith("https://") or not model:
            continue                                  # 坏数据直接忽略
        models = d.get("models")
        if isinstance(models, list) and models:
            models = [str(m) for m in models if str(m).strip()]
        else:
            models = []
        if model not in models:
            models.insert(0, model)
        out[name] = {"name": name, "label": str(d.get("label") or name),
                     "base_url": url, "default_model": model,
                     "models": models, "builtin": False, "key_env": "", "compat": []}
    return out


def upsert_custom(name: str, *, label: str, base_url: str,
                  default_model: str) -> None:
    """新增或更新一个自定义 embedding 服务商。内置名**不可覆盖**。"""
    name = (name or "").strip()
    if not name or name in BUILTINS:
        raise ValueError(f"名称 {name!r} 不可用（空或与内置重名）")
    if not base_url.startswith("https://"):
        raise ValueError("端点必须是 https:// 开头的地址")
    if not default_model.strip():
        raise ValueError("默认模型不能为空")
    provs = load_custom()
    provs[name] = {"label": label or name, "base_url": base_url,
                   "default_model": default_model}
    save_custom(provs)


def delete_custom(name: str) -> None:
    """删除自定义服务商（内置项**不可删除**；同时清掉它的 `embed:` 密钥）。"""
    if name in BUILTINS:
        raise ValueError(f"{name!r} 是内置服务商，不可删除")
    provs = load_custom()
    if provs.pop(name, None) is not None:
        save_custom(provs)
    try:
        from core import credentials
        # ★ 只删 `embed:<name>` —— 同名 LLM 服务商的密钥**原样保留**
        credentials.delete_key(key_name(name))
    except Exception:                                        # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 默认服务商（`EMBEDDING_PROVIDER`）
# ---------------------------------------------------------------------------

def default_provider() -> str:
    """当前默认 Embedding 服务商名（**函数名与 `core.providers.default_provider` 对齐**，
    界面据此用同一行代码取两类服务商的默认值）。

    **未配置 / 名字未登记时返回空串** —— 空串 = 沿用旧三件套（硬规则①）。
    名字未登记时记一条 warning（不猜、不崩）。
    """
    raw = (getattr(AppConfig, "EMBEDDING_PROVIDER", "") or "").strip()
    if not raw:
        return ""
    if raw in all_specs():
        return raw
    log.warning("[embedding] EMBEDDING_PROVIDER=%r 未登记，按**旧三件套**处理"
                "（不会自动挑一家，也不会崩）", raw)
    return ""


def set_default(name: str) -> None:
    """把某服务商设为默认 —— 写 `EMBEDDING_PROVIDER` 到 `.env` 并热更新。

    ★ 刻意**不**清 `EMBEDDING_MODEL`/`EMBEDDING_BASE_URL`：它们是旧路径的配置，
      留在文件里才能在"把 `EMBEDDING_PROVIDER` 清空"时**原样切回**旧三件套。
      （新路径下这两个键不生效，见 `resolve()`。）
    """
    name = (name or "").strip()
    if name not in all_specs():
        raise ValueError(f"未登记的服务商 {name!r}")
    from core.config_manager import ConfigManager
    from config import reload_config

    ConfigManager.save({"EMBEDDING_PROVIDER": name})
    reload_config()


# ---------------------------------------------------------------------------
# 解析 —— 全项目"用哪家 embedding"的唯一入口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EmbeddingConfig:
    """一次解析的结果。`provider == ""` 表示**旧三件套路径**（逐字回退）。"""

    provider: str          # 登记名；空串 = legacy
    base_url: str
    model: str
    api_key: str

    @property
    def is_legacy(self) -> bool:
        return not self.provider


#: 已经警告过"旧键不再生效"的 (服务商, 模型) —— 只吵一次，不刷屏
_WARNED: set[tuple[str, str]] = set()


def _key_for(name: str) -> str:
    """取某服务商的密钥：凭据层（`embed:<name>` → 环境变量）。"""
    try:
        from core import credentials
        return credentials.get_key(key_name(name)) or ""
    except Exception:                                        # noqa: BLE001
        return ""


def resolve() -> EmbeddingConfig:
    """解析当前该用哪家 Embedding。**这是"换服务商"的唯一入口。**

    三种情形（与 `providers.default_client()` 的结构刻意一致）：
      ① `EMBEDDING_PROVIDER` **留空**（默认）→ 原样返回旧三件套，
         **一个字符都不加工**（不 strip、不补斜杠）—— 否则知识库指纹会变、
         旧用户的库会被无谓重建。
      ② 设为已登记名 → 端点/模型取自登记表，密钥取自凭据层 `embed:<名称>`。
      ③ 名字未登记 → 当作 ①（`default_provider()` 已记 warning）。
    """
    name = default_provider()
    if not name:
        return EmbeddingConfig(
            provider="",
            base_url=AppConfig.EMBEDDING_BASE_URL,
            model=AppConfig.EMBEDDING_MODEL,
            api_key=AppConfig.EMBEDDING_API_KEY,
        )

    spec = all_specs().get(name) or {}
    # 模型取该服务商的 `default_model`（界面里可改，就是改这个字段）。
    # 旧键 `EMBEDDING_MODEL` 在新路径下**不生效** —— 见下方的一次性 warning。
    model = str(spec.get("default_model") or "") or AppConfig.EMBEDDING_MODEL
    base_url = str(spec.get("base_url") or "")

    legacy_model = (AppConfig.EMBEDDING_MODEL or "")
    legacy_url = (AppConfig.EMBEDDING_BASE_URL or "")
    if legacy_model and legacy_model != model and (name, model) not in _WARNED:
        _WARNED.add((name, model))
        log.warning(
            "[embedding] 已选用服务商 %r（模型 %s），旧键 EMBEDDING_MODEL=%r "
            "不再生效；要改模型请在该服务商的编辑页里改。",
            name, model, legacy_model,
        )
    if legacy_url and legacy_url != base_url and (name, "url") not in _WARNED:
        _WARNED.add((name, "url"))
        log.warning(
            "[embedding] 已选用服务商 %r（端点 %s），旧键 EMBEDDING_BASE_URL=%r "
            "不再生效；把 EMBEDDING_PROVIDER 清空即可原样切回。",
            name, base_url, legacy_url,
        )

    return EmbeddingConfig(provider=name, base_url=base_url,
                           model=model, api_key=_key_for(name))


def make_embeddings():
    """按当前配置构造 `OpenAIEmbeddings`（三个调用点共用的**唯一**构造处）。

    `check_embedding_ctx_length=False` 是原实现就有的开关，保留 ——
    少数网关不支持 langchain 的 token 预切分（会报 400），关掉才是兼容 OpenAI 的标准路径。
    """
    cfg = resolve()
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        check_embedding_ctx_length=False,
    )


def model_signature() -> str:
    """知识库指纹里的模型签名：`端点|模型`（**格式与改造前逐字一致**）。

    ★ 为什么签名里**不**带服务商名：向量只由"端点 + 模型"决定。
      换一家但端点和模型名都相同（或同一个代理的另一个 key）时**不该重建** ——
      重建要花真金白银的 embedding 费用。
    """
    cfg = resolve()
    return f"{cfg.base_url}|{cfg.model}"


# ---------------------------------------------------------------------------
# 精排（rerank）—— 与 Embedding **解耦**
# ---------------------------------------------------------------------------

def rerank_base_url() -> str:
    """精排端点：显式 `RERANK_BASE_URL` > 当前 Embedding 的 base_url。"""
    explicit = (getattr(AppConfig, "RERANK_BASE_URL", "") or "").strip()
    return explicit or resolve().base_url


def rerank_api_key() -> str:
    """精排密钥：显式 `RERANK_API_KEY` > 当前 Embedding 的密钥。"""
    explicit = (getattr(AppConfig, "RERANK_API_KEY", "") or "").strip()
    return explicit or resolve().api_key


def rerank_url() -> str:
    """精排的完整地址。

    ⚠️ 这个 `+ "/rerank"` 是**硅基流动专有的接口形状**，不是 OpenAI 兼容标准。
       换一家 embedding 时，请用 `RERANK_BASE_URL` 把它指回原来那家。
    """
    return rerank_base_url().rstrip("/") + "/rerank"


# ---------------------------------------------------------------------------
# 拉模型清单 / 测试连接（界面「测试连接」用；复用 providers 的语义）
# ---------------------------------------------------------------------------

def list_models(name: str, *, timeout_s: float = 20.0
                ) -> tuple[bool, str, list[str]]:
    """拉一次 `GET /models`，返回 `(ok, 说明, 模型 id 列表)`。

    ★ 与 `providers.list_models()` **同一套四类失败判据**（401/404/429/超时）：
      "端点/空间不匹配"也报 401，错误信息会骗人，所以分类比"能用/不能用"更有用。
    ★ 模型名各家格式不同（`text-embedding-3-small` / `BAAI/bge-m3`），
      手抄最容易错一个字符，而错误要到第一次建库才暴露 —— 所以给可选下拉。
    """
    try:
        spec = all_specs().get(name)
        if spec is None:
            return False, f"未知服务商 {name!r}", []
        from core import credentials
        key = credentials.get_key(key_name(name)) or ""
        if not key:
            return False, "未配置密钥", []
        from openai import OpenAI
        cli = OpenAI(api_key=key, base_url=spec["base_url"], timeout=timeout_s)
        page = cli.models.list()
        ids = [getattr(m, "id", "") for m in getattr(page, "data", [])]
        ids = [str(i) for i in ids if i]
        return True, f"连接正常，可用模型 {len(ids)} 个", ids
    except Exception as e:                                   # noqa: BLE001
        status = getattr(e, "status_code", None)
        if status == 401:
            return False, "密钥无效，或与端点/空间不匹配（401）", []
        if status == 404:
            return False, "端点地址不通（404）", []
        if status == 429:
            return False, "被限流（429），稍后重试", []
        name_err = type(e).__name__.lower()
        if "timeout" in name_err:
            return False, f"连接超时（>{timeout_s:.0f}s）", []
        if "connection" in name_err:
            return False, "网络不通，或端点地址错误", []
        return False, f"{type(e).__name__}: {str(e)[:120]}", []


def test_connection(name: str, *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """测试连接 —— `list_models()` 的薄封装（失败分类只有一份）。"""
    ok, detail, _models = list_models(name, timeout_s=timeout_s)
    return ok, detail