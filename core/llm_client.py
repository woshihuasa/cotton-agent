"""
大模型客户端模块
封装 OpenAI 兼容接口，提供统一的对话生成方法。
支持普通调用和流式生成两种模式。
"""

import threading
from dataclasses import dataclass
from typing import Any, Generator

from openai import APIError, OpenAI

from config import AppConfig


# ---------------------------------------------------------------------------
# 思考长度档位（2026-10-18 新增）
# ---------------------------------------------------------------------------
#
# 【为什么需要"映射表"而不是直接把界面上的字发出去】
#
#   界面用四档：`off` / `low` / `high` / `max`。
#   但接口**不接受 `off`** —— 实测 `reasoning_effort="off"` 直接返回
#   `400 invalid_parameter_error`。真正表示"关掉思考"的取值是 **`none`**。
#
#   如果当初想当然地把 `off` 原样发出去，后果是**每一次提问都失败**，不是偶发。
#
# 【实测依据】（`qwen3.8-flash` + `deepseek-v4-flash` 都验过）
#
#     取值        思考字符数(同一道推理题)     结论
#     none        0                          ✅ 真正关闭
#     low         194—239                    ✅ 生效
#     high        549—2848                   ✅ 生效
#     max         754—4490                   ✅ 生效
#     off         ——                         ❌ 400 非法
#
#   ★ 还有一条容易误解的行为：**不传任何参数时模型默认仍在思考**
#     （实测 647—936 字符）。所以"取消勾选深度思考"以前并**没有**关掉思考，
#     只是没显式打开。现在 `off` 会显式发 `none`，才算真的关。
REASONING_EFFORT_LEVELS: tuple[str, ...] = ("off", "low", "high", "max")

#: 界面档位 → 接口真正接受的取值
REASONING_EFFORT_API: dict[str, str] = {
    "off": "none",          # ★ 注意不是 "off"（那个会 400）
    "low": "low",
    "high": "high",
    "max": "max",
}

#: 默认档位 —— **High**（2026-10-18 按用户要求定为产品默认）。
#:
#: ★ 注意作用范围：这个常量只被 `RAGEngine.ask()` 用作回退值，
#:   **不给 `stream_response()` 当默认** —— 否则标题生成、摘要压缩、
#:   L4 冲突消解这些**内部维护**调用也会被顺带加价（它们不需要长思考）。
DEFAULT_REASONING_EFFORT = "high"


@dataclass
class Usage:
    """一次（或累计）LLM 调用的 token 计量。

    为什么单独建模（2026-10 新增）：本项目此前**完全丢弃了响应里的 usage**，
    因此「DeepSeek 上下文缓存命中多少」这个**可直接折算成钱**的问题从未被测量过。
    缓存命中 token 的单价显著低于未命中，而命中与否取决于 **prompt 前缀是否逐字节相同**
    —— 所以这个计量是「提示分层是否有效」的唯一直接证据。

    字段兼容两种来源：DeepSeek 原生 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，
    以及 OpenAI 风格的 `prompt_tokens_details.cached_tokens`。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    calls: int = 0

    @property
    def prompt_total(self) -> int:
        """prompt token 总数（优先用 API 给的 `prompt_tokens`，缺失时用 命中+未命中 兜底）。"""
        return self.prompt_tokens or (self.cache_hit_tokens + self.cache_miss_tokens)

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率 = 命中 / (命中 + 未命中)。无缓存数据时返回 0.0（**不是**猜测值）。"""
        denom = self.cache_hit_tokens + self.cache_miss_tokens
        return (self.cache_hit_tokens / denom) if denom else 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
            calls=self.calls + other.calls,
        )

    def describe(self) -> str:
        """一行诊断文本（供 `[L1 组装]` 风格的日志使用）。"""
        if not self.calls:
            return "无调用"
        if not (self.cache_hit_tokens or self.cache_miss_tokens):
            # 该接口没有回传缓存字段 —— 如实说明，不要伪装成 0% 命中
            return (f"{self.calls} 次 | prompt {self.prompt_total} tok "
                    f"| completion {self.completion_tokens} tok | 缓存字段缺失")
        return (f"{self.calls} 次 | prompt {self.prompt_total} tok "
                f"(命中 {self.cache_hit_tokens} / 未命中 {self.cache_miss_tokens}) "
                f"| 命中率 {self.cache_hit_rate:.1%} | completion {self.completion_tokens} tok")


def _extract_cache_tokens(raw: Any) -> tuple[int, int]:
    """从 API 的 usage 对象里取 (缓存命中, 缓存未命中)；取不到就返回 (0, 0)。

    三种取值路径都试（Pydantic 对未声明字段的处理方式因 SDK 版本而异）：
      ① DeepSeek 原生属性 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
      ② OpenAI 风格 `prompt_tokens_details.cached_tokens`（命中可从总额反推未命中）
      ③ `model_extra`（Pydantic v2 把未声明字段放在这里）
    """
    def _int(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    extra = getattr(raw, "model_extra", None) or {}
    hit = getattr(raw, "prompt_cache_hit_tokens", None)
    miss = getattr(raw, "prompt_cache_miss_tokens", None)
    if hit is None:
        hit = extra.get("prompt_cache_hit_tokens")
    if miss is None:
        miss = extra.get("prompt_cache_miss_tokens")

    if hit is None:
        details = getattr(raw, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if cached is None and details is not None:
            cached = (getattr(details, "model_extra", None) or {}).get("cached_tokens")
        if cached is not None:
            hit = cached
            total = _int(getattr(raw, "prompt_tokens", 0))
            miss = max(0, total - _int(cached))

    return _int(hit), _int(miss)


class LLMClient:
    """DeepSeek 大模型客户端，基于 OpenAI 兼容接口。

    提供两种调用方式：
      - generate_response(): 一次性返回完整回答（用于查询重写等非流式场景）
      - stream_response():    流式生成器，逐块 yield（用于 UI 实时显示）
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, extra_body: dict | None = None,
                 temperature: float | None = None) -> None:
        """初始化 OpenAI 客户端，指向 DeepSeek 的 base_url。

        Args:
            api_key: 可选的**实例级** API Key。Web 服务场景下由用户在页面填写、
                经请求头透传至此，实现"用户自备 Key"；为 None 时回退全局配置
                （桌面端行为不变）。
            base_url: 可选的服务地址覆盖，语义同上。
            model: 可选的模型覆盖。为 None 时回退 `AppConfig.DEEPSEEK_MODEL`
                —— **默认行为与改动前逐字一致**。
            extra_body: 可选的请求体附加字段。★ 用于承载"某家**必须**显式传什么"，
                例如 GLM 系列不传 `tool_stream` 会让**工具调用静默失效**（不报错）。
                这些参数集中在 `core/providers.py` 的登记表里，不散落在调用处。
            temperature: 可选的采样温度。**默认 None = 不传该参数**，
                由接口用它的默认值 —— 即**行为与改动前逐字一致**。
                ★ 评测 judge 应当显式设定它（并配合固定 `seed`），否则重跑会得到不同结果，
                就**分不清"系统变了"还是"裁判手抖了"**（见《08_提示词设计与迭代》缺陷 4）。
        """
        self._client = OpenAI(
            api_key=api_key or AppConfig.DEEPSEEK_API_KEY,
            base_url=base_url or AppConfig.DEEPSEEK_BASE_URL,
        )
        self._model = model or AppConfig.DEEPSEEK_MODEL
        self._extra_body: dict = dict(extra_body or {})
        self._temperature = temperature

        # token 计量（2026-10 新增）。刻意做成**实例属性**而不是改方法签名：
        # 三个方法的签名与返回类型**保持不变**，因此既有调用方与守卫零改动。
        #
        # ⚠️ 这三行必须留在 `__init__` 里、且**不能**被插到别的方法之后 ——
        # 曾因为把新方法插在它们前面，使其落到 `return` 之后成为死代码，
        # 表现为 `'LLMClient' object has no attribute '_usage_lock'`（仅运行时暴露）。
        self.last_usage: Usage | None = None      # 最近一次调用的计量
        self.usage_total: Usage = Usage()          # 累计
        self._usage_lock = threading.Lock()        # L4 worker 在线程里跑，累计需加锁

    def _sampling_kwargs(self) -> dict:
        """采样与附加字段。**默认为空 dict** —— 不传任何东西，保持原行为。"""
        kw: dict = {}
        if self._temperature is not None:
            kw["temperature"] = self._temperature
        if self._extra_body:
            kw["extra_body"] = dict(self._extra_body)
        return kw

    # ---- token 计量（只记录，绝不改变返回值）----

    def _record_usage(self, raw: Any) -> None:
        """把 API 响应的 usage 记进 `last_usage` 与 `usage_total`。"""
        if raw is None:
            return
        hit, miss = _extract_cache_tokens(raw)
        def _int(v: Any) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
        u = Usage(
            prompt_tokens=_int(getattr(raw, "prompt_tokens", 0)),
            completion_tokens=_int(getattr(raw, "completion_tokens", 0)),
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
            calls=1,
        )
        with self._usage_lock:
            self.last_usage = u
            self.usage_total = self.usage_total + u

    def reset_usage(self) -> None:
        """清零累计计量（供评测/守卫按轮统计）。"""
        with self._usage_lock:
            self.last_usage = None
            self.usage_total = Usage()

    def generate_response(self, messages: list) -> str:
        """（非流式）向大模型发送对话请求并一次性返回完整响应。

        Args:
            messages: 标准消息列表，每项为 {"role": ..., "content": ...}。

        Returns:
            模型返回的完整文本内容；若发生异常则返回友好的错误提示字符串。
        """
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                **self._sampling_kwargs(),
            )
            self._record_usage(getattr(response, "usage", None))
            return response.choices[0].message.content

        except APIError as e:
            print(f"[LLMClient] OpenAI API 调用失败：{e}")
            return "抱歉，AI 服务暂时不可用，请检查网络或 API 配置。"

        except Exception as e:
            print(f"[LLMClient] 未知错误：{e}")
            return "抱歉，AI 服务暂时不可用，请检查网络或 API 配置。"

    def chat_with_tools(
        self,
        messages: list,
        tools: list[dict],
        model_name: str | None = None,
    ) -> Any:
        """(非流式) 带工具定义的对话请求, 返回 message 对象。

        Args:
            messages: 标准消息列表。
            tools: DeepSeek 兼容的工具 schema 列表。
            model_name: 模型名称, None 则使用默认。

        Returns:
            response.choices[0].message, 可能含 content / tool_calls;
            异常时返回 None。
        """
        model = model_name or self._model
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                stream=False,
                **self._sampling_kwargs(),
            )
            self._record_usage(getattr(response, "usage", None))
            return response.choices[0].message
        except APIError as e:
            print(f"[LLMClient] chat_with_tools API 失败: {e}")
            return None
        except Exception as e:
            print(f"[LLMClient] chat_with_tools 未知错误: {e}")
            return None

    def stream_response(
        self,
        messages: list,
        model_name: str | None = None,
        enable_thinking: bool = False,
        reasoning_effort: str | None = None,
    ) -> Generator[tuple[str, str], None, None]:
        """（流式）向大模型发送对话请求，逐块 yield (类型, 文本) 元组。

        Args:
            messages: 标准消息列表。
            model_name: 模型名称，为 None 时回退 AppConfig.DEEPSEEK_MODEL。
            enable_thinking: 是否开启深度思考模式（**旧开关**，二值）。
            reasoning_effort: **思考长度档位**（`off` / `low` / `high` / `max`，
                由 `REASONING_EFFORT_API` 映射成接口真正接受的取值）。

        ★ `reasoning_effort` 优先于 `enable_thinking`：两者同时传会让服务端
          语义不清（一边说"开思考"一边说"思考长度=关"），所以这里用 `elif`
          保证**只发一个**。

        Yields:
            ("reasoning", text) — 模型思考过程片段；
            ("content",   text) — 模型最终回答片段。
        """
        model = model_name or self._model

        kwargs: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        # 附加字段与思考参数 **合并**，不能互相覆盖
        body = dict(self._extra_body)
        api_effort = REASONING_EFFORT_API.get(reasoning_effort or "")
        if api_effort:
            body["reasoning_effort"] = api_effort
        elif enable_thinking:
            body["thinking"] = {"type": "enabled"}
        if body:
            kwargs["extra_body"] = body

        try:
            response = self._client.chat.completions.create(**kwargs)
            for chunk in response:
                # 尾块可能**只带 usage、choices 为空** —— 先记 usage 再判空，
                # 否则 `chunk.choices[0]` 会 IndexError（本行同时是防御性修复）
                if getattr(chunk, "usage", None) is not None:
                    self._record_usage(chunk.usage)
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # 思考过程（DeepSeek 推理内容）
                rc = getattr(delta, "reasoning_content", None)
                if rc is not None:
                    yield ("reasoning", rc)

                # 最终回答
                if delta.content is not None:
                    yield ("content", delta.content)

        except APIError as e:
            print(f"[LLMClient] 流式 API 调用失败：{e}")
            # ⚠️ 必须用 **"error"** 而不是 "content"（2026-10-08 修）：
            # 原先这里 yield ("content", "抱歉，AI 服务暂时不可用…")，等于**把一句错误
            # 提示伪装成模型的回答**。后果是调用方无法区分"模型答得差"与"根本没答"：
            #   · 评测里 `answer` 非空 → 被送去 judge → covers=False，
            #     看起来像质量问题，实际是 API 挂了；
            #   · 该文案不含任何拒答特征词 → `is_refusal=False` → 被记为"拒答正确"，
            #     **拒答率虚高**（实测：API 全挂时报出 2/2 = 100%）。
            # 改为独立类型后，`ask()` 只把 "content" 计入回答，失败样本回答为空，
            # 评测可据此判为链路异常并排除出分母。
            # UI 侧无需改动：`main_window._on_update_text` 只特判 "reasoning"，
            # 其余类型同样进正文气泡，显示效果与修复前一致。
            yield ("error", "抱歉，AI 服务暂时不可用，请检查网络或 API 配置。")

        except Exception as e:
            print(f"[LLMClient] 流式未知错误：{e}")
            yield ("error", "抱歉，AI 服务暂时不可用，请检查网络或 API 配置。")
