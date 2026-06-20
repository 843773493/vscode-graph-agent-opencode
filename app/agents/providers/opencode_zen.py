from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


class OpencodeZenChatOpenAI(ChatOpenAI):
    """支持 opencode_zen 接口的 ChatOpenAI，能在流式输出中正确暴露 reasoning_content。

    背景：opencode.ai（deepseek-v4-flash 后端）在流式响应中返回 reasoning_content 字段，
    但标准 LangChain ChatOpenAI._astream 会将其丢弃，导致前端出现 "LLM 空响应"。

    本类通过重写 _astream 方法（BaseChatModel 实际调用的方法），将 reasoning 内容以统一格式
    （content + kind="reasoning"）输出，上层的 agent_execution_service 无需感知 provider 差异即可处理 reasoning。

    统一输出格式（通过 AIMessageChunk.additional_kwargs["kind"] 区分）：
        - kind="reasoning"  → 推理过程内容（放在 content 字段）
        - kind="text"       → 最终回复内容（放在 content 字段）
        - kind="tool"       → 工具调用（标准 LangChain 格式）
    """

    async def _astream(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """重写 _astream（BaseChatModel 实际调用的方法），将 reasoning_content 和 content 统一为带 kind 标记的格式。"""
        # 构造消息字典（兼容 dict 列表和 BaseMessage 列表）
        if messages and isinstance(messages[0], dict):
            message_dicts = messages
        else:
            message_dicts = self._convert_messages_to_dicts(messages)

        # opencode.ai/DeepSeek 后端要求 OpenAI 标准 role（user/assistant/system/tool），
        # 而 ChatOpenAI._convert_messages_to_dicts 会把 HumanMessage 转成 "role": "human"，
        # 把 AIMessage 转成 "role": "ai"，导致 400 错误并回退到 backup provider。
        # 这里把 LangChain 风格 role 映射回 OpenAI 标准 role。
        for msg in message_dicts:
            role = msg.get("role")
            if role == "human":
                msg["role"] = "user"
            elif role == "ai":
                msg["role"] = "assistant"
            elif role == "tool":
                msg["role"] = "tool"

        # 使用底层 OpenAI 客户端进行原始流式调用
        raw_client = self._get_raw_client()

        # 构建 OpenAI API 参数（只传递 API 接受的参数）
        api_params = {
            "model": self.model_name,
            "messages": message_dicts,
            "stream": True,
        }
        if stop is not None:
            api_params["stop"] = stop
        if self.temperature is not None:
            api_params["temperature"] = self.temperature
        if self.top_p is not None:
            api_params["top_p"] = self.top_p
        if self.max_tokens is not None:
            api_params["max_tokens"] = self.max_tokens
        # 合并 kwargs 中有效的参数
        valid_keys = {"temperature", "top_p", "max_tokens", "max_completion_tokens", "n", "presence_penalty", "frequency_penalty", "logit_bias", "user", "response_format", "seed", "tools", "tool_choice", "stream_options", "stop"}
        for k, v in kwargs.items():
            if k in valid_keys and v is not None:
                api_params[k] = v

        stream = await raw_client.chat.completions.create(**api_params)

        reasoning_started = False
        reasoning_finished = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not delta:
                continue

            # 处理 reasoning_content
            delta_reasoning = getattr(delta, "reasoning_content", None)
            if delta_reasoning:
                if not reasoning_started:
                    reasoning_started = True
                    # 发送 reasoning_start 标记
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            additional_kwargs={"kind": "reasoning", "phase": "start"},
                        )
                    )
                # 将 reasoning 内容放在 content 字段，附加 kind 标记
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta_reasoning,
                        additional_kwargs={"kind": "reasoning", "phase": "delta"},
                    )
                )

            # 处理 content
            delta_content = getattr(delta, "content", None)
            if delta_content:
                if reasoning_started and not reasoning_finished:
                    reasoning_finished = True
                    # 发送 reasoning_end 标记
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            additional_kwargs={"kind": "reasoning", "phase": "end"},
                        )
                    )
                # 将 content 放在 content 字段，附加 kind 标记
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta_content,
                        additional_kwargs={"kind": "text", "phase": "delta"},
                        response_metadata=getattr(chunk, "response_metadata", None) or {},
                    )
                )

            # 处理 tool_calls：opencode.ai 返回的 OpenAI 格式 tool_calls 需要
            # 通过 LangChain 的 tool_call_chunks 字段透传给上层，否则 LLM 的
            # 工具调用决策会被静默丢弃，导致 tool_call_start 事件永远不触发。
            delta_tool_calls = getattr(delta, "tool_calls", None)
            if delta_tool_calls:
                tool_call_chunks: list[dict[str, Any]] = []
                for rtc in delta_tool_calls:
                    function = getattr(rtc, "function", None)
                    chunk_dict: dict[str, Any] = {
                        "name": getattr(function, "name", None) if function else None,
                        "args": getattr(function, "arguments", None) if function else None,
                        "id": getattr(rtc, "id", None),
                        "index": getattr(rtc, "index", None),
                    }
                    tool_call_chunks.append(chunk_dict)
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
                    )
                )

        # 如果流结束但 content 为空（reasoning-only 场景）
        if reasoning_started and not reasoning_finished:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "end"},
                )
            )

    def _convert_messages_to_dicts(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """将 BaseMessage 列表转换为 OpenAI API 格式的字典列表。"""
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, dict):
                result.append(msg)
            else:
                result.append({"role": msg.type, "content": msg.content})
        return result

    def _get_raw_client(self):
        """获取底层的 AsyncOpenAI 实例。"""
        # ChatOpenAI 内部通过 root_async_client 持有 AsyncOpenAI 实例
        client = getattr(self, "root_async_client", None)
        if client is None:
            raise RuntimeError("ChatOpenAI 异步客户端未初始化")
        return client
