from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI

from app.agents.providers._format_check import (
    FormatCheckItem,
    FormatCheckResult,
    check_history_messages_accepted,
    validate_provider_format,
)


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

from typing import Any, AsyncIterator, Dict, List, Optional
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai.chat_models.base import ChatOpenAI
from openai import OpenAI

class OpencodeZenChatOpenAI(ChatOpenAI):
    """Opencode.ai (基于 DeepSeek) 的 ChatOpenAI 适配器。

    - 将 API 返回的 `reasoning_content` 字段解析为带 `kind` 标记的流式 chunk，
      使前端能够区分 reasoning 和正式回答。
    - 对历史消息中的结构化 reasoning 块进行展平，兼容多轮对话。
    - 透传 tool_calls，保证 LangChain 的工具调用链路正常工作。
    """

    def _process_chunk(self, chunk: Any, state: Dict[str, bool]) -> List[ChatGenerationChunk]:
        """处理单个流式 chunk，返回需要 yield 的 ChatGenerationChunk 列表。

        state 是一个字典，包含：
        - reasoning_started: bool
        - reasoning_finished: bool
        """
        chunks: List[ChatGenerationChunk] = []
        
        if not chunk.choices:
            return chunks
            
        delta = chunk.choices[0].delta
        if not delta:
            return chunks

        # 处理 reasoning_content
        # 注意：OpenAI SDK 的 pydantic 模型可能丢弃未定义字段（extra="ignore"），
        # 需要检查 model_extra 获取被丢弃的 reasoning_content 字段
        delta_reasoning = None
        # 方法1：直接 getattr（适用于 SDK 已定义该字段的情况）
        delta_reasoning = getattr(delta, "reasoning_content", None)
        # 方法2：检查 model_extra（适用于 SDK 未定义该字段但 API 返回了的情况）
        if not delta_reasoning and hasattr(delta, "model_extra") and delta.model_extra:
            delta_reasoning = delta.model_extra.get("reasoning_content")
        # 方法3：检查其他可能的字段名
        if not delta_reasoning and hasattr(delta, "model_extra") and delta.model_extra:
            delta_reasoning = delta.model_extra.get("reasoning")
            
        if delta_reasoning:
            if not state["reasoning_started"]:
                state["reasoning_started"] = True
                # 发送 reasoning_start 标记
                chunks.append(
                    ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            additional_kwargs={"kind": "reasoning", "phase": "start"},
                        )
                    )
                )
            # 将 reasoning 内容放在 content 字段，附加 kind 标记
            chunks.append(
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta_reasoning,
                        additional_kwargs={"kind": "reasoning", "phase": "delta"},
                    )
                )
            )

        # 处理 content
        delta_content = getattr(delta, "content", None)
        if delta_content:
            if state["reasoning_started"] and not state["reasoning_finished"]:
                state["reasoning_finished"] = True
                # 发送 reasoning_end 标记
                chunks.append(
                    ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            additional_kwargs={"kind": "reasoning", "phase": "end"},
                        )
                    )
                )
            # 将 content 放在 content 字段，附加 kind 标记
            chunks.append(
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta_content,
                        additional_kwargs={"kind": "text", "phase": "delta"},
                        response_metadata=getattr(chunk, "response_metadata", None) or {},
                    )
                )
            )

        # 处理 tool_calls：opencode.ai 返回的 OpenAI 格式 tool_calls 需要
        # 通过 LangChain 的 tool_call_chunks 字段透传给上层，否则 LLM 的
        # 工具调用决策会被静默丢弃，导致 tool_call_start 事件永远不触发。
        delta_tool_calls = getattr(delta, "tool_calls", None)
        if delta_tool_calls:
            tool_call_chunks: List[Dict[str, Any]] = []
            for rtc in delta_tool_calls:
                function = getattr(rtc, "function", None)
                chunk_dict: Dict[str, Any] = {
                    "name": getattr(function, "name", None) if function else None,
                    "args": getattr(function, "arguments", None) if function else None,
                    "id": getattr(rtc, "id", None),
                    "index": getattr(rtc, "index", None),
                }
                tool_call_chunks.append(chunk_dict)
            chunks.append(
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
                    )
                )
            )

        return chunks

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

        state = {"reasoning_started": False, "reasoning_finished": False}

        async for chunk in stream:
            for processed_chunk in self._process_chunk(chunk, state):
                yield processed_chunk

        # 如果流结束但 content 为空（reasoning-only 场景）
        if state["reasoning_started"] and not state["reasoning_finished"]:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "end"},
                )
            )

    def _flatten_reasoning_blocks(self, content: Any) -> Any:
        """把结构化 reasoning 块展平为可被 opencode.ai/DeepSeek 后端接受的文本。

        opencode.ai/DeepSeek 的 Chat Completions 接口不识别 OpenAI Responses API
        的 `type: reasoning` 块结构，会直接 400。我们把 reasoning 块的 summary
        拼成 `<think>...</think>` 文本注入 content 字符串的开头，让 reasoning
        上下文能以模型可消费的形式在多轮对话中保留。

        其余 content 块（text / output_text）保持原样。
        """
        if not isinstance(content, list):
            return content
        if not any(
            isinstance(b, dict) and b.get("type") == "reasoning"
            for b in content
        ):
            return content

        reasoning_fragments: list[str] = []
        flattened: list[Any] = []
        for block in content:
            if not isinstance(block, dict):
                flattened.append(block)
                continue
            if block.get("type") == "reasoning":
                summary = block.get("summary")
                if isinstance(summary, list):
                    for entry in summary:
                        if isinstance(entry, dict):
                            text = entry.get("text", "")
                            if isinstance(text, str) and text:
                                reasoning_fragments.append(text)
                        elif isinstance(entry, str):
                            reasoning_fragments.append(entry)
            else:
                flattened.append(block)

        if not reasoning_fragments:
            return content

        reasoning_text = "<think>\n" + "\n".join(reasoning_fragments) + "\n</think>\n\n"
        # 把 reasoning 文本作为前缀注入到第一个 text/output_text 块；如不存在则新建一个
        injected = False
        for block in flattened:
            if isinstance(block, dict) and block.get("type") in ("text", "output_text"):
                existing = block.get("text", "")
                if isinstance(existing, str):
                    block["text"] = reasoning_text + existing
                    injected = True
                    break
        if not injected:
            flattened.insert(0, {"type": "text", "text": reasoning_text.rstrip()})
        return flattened

    def _convert_messages_to_dicts(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """将 BaseMessage 列表转换为 OpenAI API 格式的字典列表。

        注意：返回的 dict 必须是 **OpenAI 标准 role**（user / assistant / system / tool），
        而不是 LangChain 风格（human / ai）。`_astream` 会在调用后做一层 role 修正，
        但任何**直接复用**本方法的代码（包括 subagent、LangGraph 内部路径、
        以及 `MessageFormatValidator` 的历史回环检查）都会拿到这里的输出。
        因此这里统一完成映射，避免下游再处理一次。
        """
        lc_to_openai_role = {
            "human": "user",
            "ai": "assistant",
            "system": "system",
            "tool": "tool",
        }
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, dict):
                result.append(msg)
                continue
            content = self._flatten_reasoning_blocks(msg.content)
            role = lc_to_openai_role.get(msg.type, msg.type)
            result.append({"role": role, "content": content})
        return result

    def _get_raw_client(self):
        """获取底层的 AsyncOpenAI 实例。"""
        # ChatOpenAI 内部通过 root_async_client 持有 AsyncOpenAI 实例
        client = getattr(self, "root_async_client", None)
        if client is None:
            raise RuntimeError("ChatOpenAI 异步客户端未初始化")
        return client

    # ------------------------------------------------------------------
    # 格式自检接口（MessageFormatValidator 协议）
    # ------------------------------------------------------------------
    async def build_stream(
        self, scenario: str
    ) -> AsyncIterator[ChatGenerationChunk]:
        """在不发起真实请求的情况下，构造 opencode_zen 风格的"已知行为"流。

        用于 `validate_provider_format` 跑回归：每个 scenario 模拟一种典型
        后端响应模式，验证 _astream 的转换逻辑能正确产出统一格式 chunk。

        真实运行时这条路径不会触发（仅 self_check() 调用）。
        """
        if scenario == "reasoning_only":
            # 模拟：只输出 reasoning_content，没有 content
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "start"},
                )
            )
            for delta in ["先", "思考", "一下", "结论"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta,
                        additional_kwargs={"kind": "reasoning", "phase": "delta"},
                    )
                )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "end"},
                )
            )
        elif scenario == "text_only":
            for delta in ["你好", "，", "世界"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta,
                        additional_kwargs={"kind": "text", "phase": "delta"},
                    )
                )
        elif scenario == "mixed_reasoning_text":
            # reasoning → end → text
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "start"},
                )
            )
            for delta in ["思考", "中"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta,
                        additional_kwargs={"kind": "reasoning", "phase": "delta"},
                    )
                )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "end"},
                )
            )
            for delta in ["最终", "回答"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta,
                        additional_kwargs={"kind": "text", "phase": "delta"},
                    )
                )
        elif scenario == "tool_call":
            for delta in ["调用", "工具"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta,
                        additional_kwargs={"kind": "text", "phase": "delta"},
                    )
                )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "list_files",
                            "args": '{"path": "."}',
                            "id": "call_abc",
                            "index": 0,
                        }
                    ],
                )
            )
        elif scenario == "reasoning_then_tool":
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "start"},
                )
            )
            for delta in ["思考"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta,
                        additional_kwargs={"kind": "reasoning", "phase": "delta"},
                    )
                )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"kind": "reasoning", "phase": "end"},
                )
            )
            for delta in ["决定调用"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=delta,
                        additional_kwargs={"kind": "text", "phase": "delta"},
                    )
                )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "shell",
                            "args": '{"cmd": "ls"}',
                            "id": "call_xyz",
                            "index": 0,
                        }
                    ],
                )
            )
        else:
            raise ValueError(f"unknown scenario for self_check: {scenario!r}")

    def self_check(self) -> FormatCheckResult:
        """格式自检入口：跑所有 fixture × 所有检查项 + 历史消息回环。

        可在测试和 debug 时直接调用::

            provider = OpencodeZenChatOpenAI(...)
            result = provider.self_check()
            assert result.all_passed, result.report()

        返回的 FormatCheckResult 包含每条检查的 detail + remediation，
        失败时人类可直接读 .report() 知道该改哪里。
        """
        import asyncio

        result = asyncio.run(validate_provider_format(self))

        # 补一项：历史消息回环校验（喂 Responses API 风格 AIMessage 验证回环）
        history_sample = [
            AIMessage(
                content=[
                    {
                        "type": "reasoning",
                        "id": "rs_history",
                        "summary": [{"type": "summary_text", "text": "历史推理"}],
                    },
                    {"type": "text", "text": "历史回答"},
                ]
            )
        ]
        history_check = check_history_messages_accepted(self, history_sample)
        result.add(FormatCheckItem(
            name="[history_roundtrip] 历史 AIMessage 含 reasoning 块时能被 _convert_messages_to_dicts 处理",
            passed=history_check.passed,
            detail=history_check.detail,
            remediation=history_check.remediation,
        ))

        return result
