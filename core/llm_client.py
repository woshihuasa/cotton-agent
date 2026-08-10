"""
大模型客户端模块
封装 OpenAI 兼容接口，提供统一的对话生成方法。
支持普通调用和流式生成两种模式。
"""

from typing import Any, Generator

from openai import APIError, OpenAI

from config import AppConfig


class LLMClient:
    """DeepSeek 大模型客户端，基于 OpenAI 兼容接口。

    提供两种调用方式：
      - generate_response(): 一次性返回完整回答（用于查询重写等非流式场景）
      - stream_response():    流式生成器，逐块 yield（用于 UI 实时显示）
    """

    def __init__(self) -> None:
        """初始化 OpenAI 客户端，指向 DeepSeek 的 base_url。"""
        self._client = OpenAI(
            api_key=AppConfig.DEEPSEEK_API_KEY,
            base_url=AppConfig.DEEPSEEK_BASE_URL,
        )
        self._model = AppConfig.DEEPSEEK_MODEL

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
            )
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
            )
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
    ) -> Generator[tuple[str, str], None, None]:
        """（流式）向大模型发送对话请求，逐块 yield (类型, 文本) 元组。

        Args:
            messages: 标准消息列表。
            model_name: 模型名称，为 None 时回退到 AppConfig.DEEPSEEK_MODEL。
            enable_thinking: 是否开启深度思考模式。

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
        if enable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        try:
            response = self._client.chat.completions.create(**kwargs)
            for chunk in response:
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
            yield ("content", "抱歉，AI 服务暂时不可用，请检查网络或 API 配置。")

        except Exception as e:
            print(f"[LLMClient] 流式未知错误：{e}")
            yield ("content", "抱歉，AI 服务暂时不可用，请检查网络或 API 配置。")
