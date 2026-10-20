# -*- coding: utf-8 -*-
"""模型服务商登记表 —— 「哪家、什么端点、要什么 key、有什么坑」的唯一真相。

【为什么要一张表】本项目现在接两家（DeepSeek 官方 + 千问 Token Plan），
        而**每家都有自己的坑**：
          · GLM 系列必须显式传 `tool_stream`，否则**工具调用静默失效**（不报错！）
          · Token Plan 平台 `supportsDeveloperRole = false`（不能发 `role: "developer"`）
          · 两家端点不能混（写错端点报 401，且错误信息会**误导成"key 失效"**）
        把这些散在调用代码里，就一定会有人漏。**登记表是唯一真相。**

【三条硬规则】
  ① **缺 key 必须报错，不许静默换一家** —— 否则你会在不知情的情况下用另一个模型跑评测，
     而**指标口径就变了**。宁可失败，不要静默改变语义。
  ② **judge 不走 failover** —— 评测中途换模型 => 前 10 条与后 10 条是两个评委打的分，
     **那个分数比没有分数更糟**。见 `resolve_judge()`。
  ③ **错误分两类** —— 判据是「**换一家会不会改变结果？**」
     会 → 可重试；不会 → 直接报错（例如余额不足、参数错误、鉴权失败）。

【端点来源】千问 Token Plan 的端点**不是猜的、也不是试出来的**，而是从
        DSH（DeepSeek Harness）的供应商目录里读出来的：
        `@earendil-works/pi-ai/dist/providers/data/qwen-token-plan-cn.json`。
        ⚠️ 官方文档示例给的是 `maas.qianwenaiapi.com`，那是**另一个（按量付费）**端点，
        用它调 Token Plan 的 key 会 401 —— 这个坑我们实测踩过。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig
from core.llm_client import LLMClient


@dataclass(frozen=True)
class Provider:
    """一个模型服务商。"""

    name: str                       # 内部 id（固定不可改：它是配置与日志的键）
    label: str                      # 界面上显示的名字
    base_url: str
    key_env: str                    # 从哪个环境变量取 key
    default_model: str
    models: tuple[str, ...] = ()    # 已知可用模型（供下拉；可被「拉取模型列表」刷新）
    supports_tool_calling: bool = True
    #: 「这家必须显式传什么」—— 例如 GLM 的 `tool_stream`。
    #: 放在这里而不是调用处，是为了让它**可查、可守卫**。
    extra_body: dict = field(default_factory=dict)
    #: 兼容性备注（人读；来源见模块注释）
    compat: tuple[str, ...] = ()


#: 千问 Token Plan 的模型（来源：该端点 `/models` 实测返回）。
#: ★ 含非对话模型（语音 / 图像 / 视频）—— 登记表是**完整目录**，
#:   不代表都能当对话模型用。
QWEN_MODELS: tuple[str, ...] = (
    # 路由别名
    "auto", "decision-model-preview",
    # 阿里 Qwen
    "qwen3.6-flash", "qwen3.7-max", "qwen3.7-plus",
    "qwen3.8-flash", "qwen3.8-max",
    # DeepSeek（三方托管）
    "deepseek-v4-flash-0731", "deepseek-v4-pro", "deepseek-v4-pro-0813",
    "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017",
    # 智谱 GLM
    "glm-5.2", "glm-5.3",
    # 语音
    "qwen-audio-3.0-realtime-plus", "qwen-audio-3.0-tts-plus",
    # 图像 / 视频
    "wan2.7-image", "wan2.7-image-pro", "wan3.0-video",
)

#: 登记表里**不能当对话模型用**的条目（语音 / 图像 / 视频）。
#:
#: ★ 它们**仍留在 `models` 里** —— 登记表是"这家供什么"的**完整目录**，
#:   删掉会让目录失真。过滤发生在**消费端**（界面的模型候选表），
#:   这样"目录完整"与"候选可用"两件事各自成立。
NON_CHAT_MODELS: frozenset[str] = frozenset({
    "qwen-audio-3.0-realtime-plus",
    "qwen-audio-3.0-tts-plus",
    "wan2.7-image",
    "wan2.7-image-pro",
    "wan3.0-video",
})

PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        name="deepseek",
        label="DeepSeek（官方）",
        base_url="https://api.deepseek.com",
        key_env="DEEPSEEK_API_KEY",
        # ★ 规范名以官方 `/models` 为准（其错误信息明示这两个）：
        #   `deepseek-v4-flash` 仍返回 200，但属旧别名，不登记。
        default_model="deepseek-flash",
        models=("deepseek-flash", "deepseek-v4-pro"),
    ),
    "qwen": Provider(
        name="qwen",
        label="千问 Token Plan",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        key_env="DASHSCOPE_API_KEY",
        default_model="qwen3.7-plus",
        models=QWEN_MODELS,
        compat=(
            "supportsStrictMode=true",
            "thinkingFormat=qwen（思考内容不是 OpenAI 的 reasoning_content 惯例）",
            "supportsDeveloperRole=false（★ 不能发 role:developer）",
            "supportsStore=false",
            "supportsReasoningEffort 视模型而定",
        ),
    ),
}


# ---------------------------------------------------------------------------
# 错误分类 —— failover 的核心判据
# ---------------------------------------------------------------------------

#: ★ 判据：「**换一家会不会改变结果？**」会 → 可重试；不会 → 直接报错。
RETRIABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
NON_RETRIABLE_STATUS = frozenset({400, 401, 402, 403, 404, 422})


def classify_error(exc: BaseException) -> str:
    """把异常分成 `"retriable"` / `"fatal"` / `"unknown"`。

    ★ **402（余额不足）绝不重试** —— 换一家 = 拿另一家的钱填这一家的坑。
    ★ **400（参数错误）绝不重试** —— 是我们发的内容有问题，换谁都 400。
    ★ **401（鉴权失败）绝不重试** —— 配置问题；且"地域/空间不匹配"也报 401，
      错误信息看着像"key 失效"，重试没有任何意义。
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        if status in NON_RETRIABLE_STATUS:
            return "fatal"
        if status in RETRIABLE_STATUS:
            return "retriable"
        if 500 <= status < 600:
            return "retriable"
        return "fatal"

    # 没有状态码：按异常类型判断
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "network", "apiconnection")):
        return "retriable"
    # ⚠️ 无法判断时**归为 fatal**：failover 是"多花一次钱"，不该在不确定时触发
    return "unknown"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def get(name: str) -> Provider:
    """按名字取服务商；不存在时**明确报错并列出可选项**。"""
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(
            f"未知的服务商 {name!r}；可用: {sorted(PROVIDERS)}"
        ) from None


def _key_for(p: Provider, api_key: str | None = None) -> str:
    """取 key：显式传入 > 凭据文件 > 环境变量。

    ⚠️ 凭据层用**延迟导入**（`core.credentials` 会 import config，避免模块级循环）。
    """
    if api_key:
        return api_key
    try:
        from core import credentials
        got = credentials.get_key(p.name)
        if got:
            return got
    except Exception:                                        # noqa: BLE001
        pass                                                 # 凭据层不可用时退回环境变量
    return os.getenv(p.key_env, "") or getattr(AppConfig, p.key_env, "") or ""


def resolve(name: str, *, model: str | None = None) -> LLMClient:
    """构造某服务商的客户端。**缺 key 直接报错，绝不静默换一家**（硬规则①）。"""
    p = get(name)
    key = _key_for(p)
    if not key:
        raise RuntimeError(
            f"服务商 {p.label!r}（{name}）没有可用密钥："
            f"请在设置里填写，或设置环境变量 {p.key_env}。"
            f"（**不会自动改用其它服务商** —— 那会静默改变评测口径）"
        )
    return LLMClient(
        api_key=key, base_url=p.base_url,
        model=model or p.default_model, extra_body=p.extra_body,
    )


def resolve_judge(name: str, *, model: str) -> LLMClient:
    """★ **评测 judge 专用**：与 `resolve()` 分开，**不参与 failover**（硬规则②）。

    为什么单独一个函数：评测中途换模型 => 前后两半是两个评委打的分，
    **那个"忠实度 X%"不再是同一个指标**。所以 judge 失败就失败，宁可不跑。
    """
    return resolve(name, model=model)


def known_names() -> list[str]:
    return sorted(PROVIDERS)


# ---------------------------------------------------------------------------
# 熔断器 —— 连续失败后暂时摘除，避免每次都先撞一次墙
# ---------------------------------------------------------------------------

#: 连续失败多少次后摘除
CIRCUIT_THRESHOLD = int(os.getenv("LLM_CIRCUIT_THRESHOLD", "3") or 3)
#: 摘除多少秒（之后放一次试探请求过去，成功即恢复）
CIRCUIT_COOLDOWN_S = float(os.getenv("LLM_CIRCUIT_COOLDOWN", "60") or 60)


class _Circuit:
    __slots__ = ("failures", "open_until")

    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0


_CIRCUITS: dict[str, _Circuit] = {}
_CIRCUIT_LOCK = threading.Lock()


def circuit_open(name: str) -> bool:
    """该服务商当前是否处于**摘除**状态（熔断打开）。"""
    with _CIRCUIT_LOCK:
        c = _CIRCUITS.get(name)
        return bool(c and c.open_until > _time.time())


def circuit_snapshot() -> dict[str, dict]:
    """给界面/日志用的只读快照。"""
    now = _time.time()
    with _CIRCUIT_LOCK:
        return {
            n: {"failures": c.failures,
                "open": c.open_until > now,
                "remain_s": max(0.0, round(c.open_until - now, 1))}
            for n, c in _CIRCUITS.items()
        }


def reset_circuits() -> None:
    """清空全部熔断状态（测试与"手动重试"用）。"""
    with _CIRCUIT_LOCK:
        _CIRCUITS.clear()


def _on_success(name: str) -> None:
    with _CIRCUIT_LOCK:
        _CIRCUITS.pop(name, None)          # 成功即完全恢复


def _on_failure(name: str) -> None:
    with _CIRCUIT_LOCK:
        c = _CIRCUITS.setdefault(name, _Circuit())
        c.failures += 1
        if c.failures >= CIRCUIT_THRESHOLD:
            c.open_until = _time.time() + CIRCUIT_COOLDOWN_S


# ---------------------------------------------------------------------------
# failover —— 只在"换一家可能有用"时才换
# ---------------------------------------------------------------------------

def failover_chain(primary: str | None = None,
                   extra: list[str] | None = None) -> list[str]:
    """构造尝试顺序：主服务商 → 配置的备用链（去重、去掉未登记的）。

    `LLM_FAILOVER` 环境变量用逗号分隔，例如 `qwen,deepseek`。
    """
    env = os.getenv("LLM_FAILOVER", "") or ""
    order: list[str] = []
    for name in [primary] + (extra or []) + env.split(","):
        n = (name or "").strip()
        if n and n in PROVIDERS and n not in order:
            order.append(n)
    if not order:
        order = [next(iter(PROVIDERS))]
    return order


def call_with_failover(fn, *, primary: str | None = None,
                       extra: list[str] | None = None):
    """按链顺序调用 `fn(client)`，**只对可重试错误**继续下一家。

    ★ 判据是 `classify_error()`：
      · `retriable`（429/5xx/超时）→ 换下一家
      · `fatal`（402 余额不足 / 400 参数错误 / 401 鉴权）→ **立刻抛，不换家**
      · `unknown` → **不换家**（failover 要多花一次钱，不确定时不花）

    ⚠️ 被熔断摘除的服务商会**跳过**，跳过原因会写进日志（不静默）。
    """
    log = logging.getLogger(__name__)
    order = failover_chain(primary, extra)
    last_exc: BaseException | None = None
    tried: list[str] = []

    for name in order:
        if circuit_open(name):
            log.warning("[failover] 跳过 %s：熔断中（连续失败 %s 次）",
                        name, CIRCUIT_THRESHOLD)
            continue
        try:
            client = resolve(name)
        except Exception as e:                               # noqa: BLE001
            # 缺 key 之类 —— **记日志并试下一家**，但绝不静默
            log.warning("[failover] 跳过 %s：无法构造客户端（%s）", name, e)
            continue
        tried.append(name)
        try:
            out = fn(client)
            _on_success(name)
            if name != order[0]:
                log.warning("[failover] 已切换到备用服务商 %s（主服务商不可用）", name)
            return out
        except Exception as e:                               # noqa: BLE001
            kind = classify_error(e)
            if kind == "retriable":
                _on_failure(name)
                log.warning("[failover] %s 可重试失败（%s），尝试下一家", name,
                            type(e).__name__)
                last_exc = e
                continue
            # ★ fatal / unknown 一律不换家
            log.error("[failover] %s 失败且**不可重试**（%s），直接报错", name, kind)
            raise

    raise RuntimeError(
        f"全部服务商不可用（已尝试: {tried or '无'}）；最后一次错误: "
        f"{type(last_exc).__name__ if last_exc else '无'}: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# 成本分层路由 —— ★ **默认关闭**，且 L2 永不降档
# ---------------------------------------------------------------------------

#: 开关。**默认关** —— 分层一旦判错，答案质量会下降而**用户不知道为什么**。
#: 所以它必须先**能证明自己**（`01` 号 §5.6 的方法：同一批题跑两个模型比对），才配默认开启。
COST_ROUTING = (os.getenv("LLM_COST_ROUTING", "0") or "0").strip().lower() in (
    "1", "true", "yes", "on")


#: 档位 → (provider, model)。
#: ★ **L2 刻意不在此表中** —— 全链问题必须用生产模型，**不许降档**。
COST_TIERS: dict[str, tuple[str, str]] = {
    "L0": ("qwen", "qwen3.8-flash"),      # 打招呼：不需要推理
    "L1": ("qwen", "qwen3.7-plus"),       # 单工具/单检索：要工具能力
}


def model_for_route(level: str) -> tuple[str, str] | None:
    """按档位选 (provider, model)。返回 `None` = **用生产配置**（不降档）。

    三种返回 None 的情形，都是**刻意的保守**：
      ① 开关关闭（默认）；
      ② 档位不在表里（**L2 全链**走这条）；
      ③ 该服务商未登记（配置被改坏时不要崩）。
    """
    if not COST_ROUTING:
        return None
    tier = COST_TIERS.get(str(level))
    if tier is None:
        return None
    prov, model = tier
    if prov not in PROVIDERS:
        return None
    return prov, model


#: 客户端缓存 —— 按 (provider, model) 复用，避免每轮重建（OpenAI 客户端有连接池）
_CLIENT_CACHE: dict[tuple[str, str], LLMClient] = {}


def cached_client(provider: str, model: str) -> LLMClient:
    """按 (服务商, 模型) 取一个**缓存**的客户端。"""
    key = (provider, model)
    got = _CLIENT_CACHE.get(key)
    if got is None:
        got = resolve(provider, model=model)
        _CLIENT_CACHE[key] = got
    return got


def clear_client_cache() -> None:
    """清空客户端缓存（设置变更后调用，避免用到旧 key/端点）。"""
    _CLIENT_CACHE.clear()


def default_provider() -> str:
    """当前默认服务商名。**未配置时返回空串**（= 沿用旧的 DEEPSEEK_* 配置）。"""
    try:
        from config import AppConfig
        name = (getattr(AppConfig, "LLM_PROVIDER", "") or "").strip()
    except Exception:                                        # noqa: BLE001
        name = ""
    return name if name in PROVIDERS else ""


def default_client(*, model: str | None = None,
                   api_key: str | None = None) -> LLMClient:
    """构造**全项目默认客户端**。

    ★ 这是"换服务商"的唯一入口。三种情形：
      ① `LLM_PROVIDER` **留空**（默认）→ 完全沿用旧路径 `LLMClient(...)`，
         **行为与改动前逐字一致**（读 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`）；
      ② `LLM_PROVIDER` 设为已登记名（如 `qwen`）→ 端点/密钥取自登记表与凭据层，
         模型取 `LLM_MODEL`，留空则用该服务商的 `default_model`；
      ③ 名字**未登记** → 当作 ①（不猜、不崩，并记一条警告）。

    `api_key` 传参（Web 访客自带 key）时，**端点仍用当前默认服务商**的正确端点 ——
    这样"用户自备 key"和"默认端点"不会错配。
    """
    name = default_provider()
    if not name:
        if api_key:
            return LLMClient(api_key=api_key, model=model)
        return LLMClient(model=model)

    p = get(name)
    key = api_key or _key_for(p)
    if not key:
        # ★ 缺 key 直接报错（硬规则①）—— 不静默退回 DeepSeek 官方端点，
        # 否则"以为在用 Token Plan、其实在花 DeepSeek 的钱"。
        raise RuntimeError(
            f"默认服务商设为 {name!r}（{p.label}），但没有可用密钥："
            f"请在设置里填写，或设置环境变量 {p.key_env}。"
        )
    try:
        from config import AppConfig
        mdl = model or (getattr(AppConfig, "LLM_MODEL", "") or "").strip() or p.default_model
    except Exception:                                        # noqa: BLE001
        mdl = model or p.default_model
    return LLMClient(api_key=key, base_url=p.base_url,
                     model=mdl, extra_body=p.extra_body)


# ---------------------------------------------------------------------------
# 自定义服务商（界面可增删改，持久化到运行数据区）
# ---------------------------------------------------------------------------

#: ⚠️ **内置服务商的 `base_url` 不开放编辑** —— 端点里编着"对不对"的信息
#: （地域、按量付费 vs Token Plan），改错会得到**看着像"key 失效"的 401**。
#: 内置项允许改的只有 **显示名称 / 默认模型 / API Key**（Key 走凭据层）。
EDITABLE_BUILTIN_FIELDS = ("label", "default_model")

#: 自定义服务商允许编辑的字段
CUSTOM_FIELDS = ("label", "base_url", "default_model")


def _custom_path() -> Path:
    from config import app_data_dir
    return Path(app_data_dir()) / "providers.json"


def load_custom() -> dict[str, dict]:
    """读自定义服务商。**任何异常都降级为空**（坏文件不该让应用起不来）。"""
    try:
        p = _custom_path()
        if not p.is_file():
            return {}
        data = json.loads(p.read_text("utf-8"))
        provs = data.get("providers") if isinstance(data, dict) else None
        return provs if isinstance(provs, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def save_custom(provs: dict[str, dict]) -> None:
    """原子写自定义服务商（临时文件 + `os.replace`）。"""
    p = _custom_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "providers": provs},
                         ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".prov_", suffix=".tmp")
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
    clear_client_cache()        # 端点/模型变了，旧客户端不能再用


def all_specs() -> dict[str, dict]:
    """界面用的**合并视图**：内置 + 自定义。

    每项 `{"name","label","base_url","default_model","models","builtin","key_env","compat"}`。
    自定义项**不覆盖内置项**（同名以内置为准，并标注冲突）—— 避免"改坏一个内置端点"。

    ★ 2026-10-18：补上 `models`。此前 `Provider` 数据类里**一直有这个字段**
      （千问登记了 20 个模型），但 `all_specs()` **没把它吐出来** ——
      于是界面拿不到"这家有哪些模型"，只能写死一个列表，
      结果千问配好了、下拉框里还是 DeepSeek 的两个模型。
      **数据一直在，是接口没暴露。**
    """
    out: dict[str, dict] = {}
    for name, p in PROVIDERS.items():
        out[name] = {"name": name, "label": p.label, "base_url": p.base_url,
                     "default_model": p.default_model, "builtin": True,
                     "models": list(p.models) or [p.default_model],
                     "key_env": p.key_env, "compat": list(p.compat)}
    for name, d in load_custom().items():
        if name in out or not isinstance(d, dict):
            continue
        url = str(d.get("base_url") or "")
        model = str(d.get("default_model") or "")
        if not url.startswith("https://") or not model:
            continue                                  # 坏数据直接忽略
        # 自定义服务商可选带 `models` 列表；没有就至少包含默认模型
        custom_models = d.get("models")
        if isinstance(custom_models, list) and custom_models:
            models = [str(m) for m in custom_models if str(m).strip()]
        else:
            models = []
        if model not in models:
            models.insert(0, model)
        out[name] = {"name": name, "label": str(d.get("label") or name),
                     "base_url": url, "default_model": model,
                     "models": models,
                     "builtin": False, "key_env": "", "compat": []}
    return out


def upsert_custom(name: str, *, label: str, base_url: str,
                  default_model: str) -> None:
    """新增或更新一个自定义服务商。内置名**不可覆盖**。"""
    name = (name or "").strip()
    if not name or name in PROVIDERS:
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
    """删除自定义服务商（内置项**不可删除**；同时清掉它的密钥）。"""
    if name in PROVIDERS:
        raise ValueError(f"{name!r} 是内置服务商，不可删除")
    provs = load_custom()
    if provs.pop(name, None) is not None:
        save_custom(provs)
    try:
        from core import credentials
        credentials.delete_key(name)
    except Exception:                                        # noqa: BLE001
        pass


def test_connection(name: str, *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """测试连接：调 `GET /models`（**不消耗生成 token**）。

    返回 `(ok, 说明)`。★ 区分四类失败 —— 比"能用/不能用"更有价值：
    因为"端点/空间不匹配"也报 401，错误信息**看着像"key 失效"**。
    """
    try:
        spec = all_specs().get(name)
        if spec is None:
            return False, f"未知服务商 {name!r}"
        from core import credentials
        key = credentials.get_key(name) or ""
        if not key:
            return False, "未配置密钥"
        from openai import OpenAI
        cli = OpenAI(api_key=key, base_url=spec["base_url"], timeout=timeout_s)
        page = cli.models.list()
        ids = [getattr(m, "id", "") for m in getattr(page, "data", [])]
        return True, f"连接正常，可用模型 {len(ids)} 个"
    except Exception as e:                                   # noqa: BLE001
        status = getattr(e, "status_code", None)
        if status == 401:
            return False, "密钥无效，或与端点/空间不匹配（401）"
        if status == 404:
            return False, "端点地址不通（404）"
        if status == 429:
            return False, "被限流（429），稍后重试"
        name_err = type(e).__name__.lower()
        if "timeout" in name_err:
            return False, f"连接超时（>{timeout_s:.0f}s）"
        if "connection" in name_err:
            return False, "网络不通，或端点地址错误"
        return False, f"{type(e).__name__}: {str(e)[:120]}"