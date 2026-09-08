"""LiteLLM history/context message 到 provider request 的 projection owner。"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ChatMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agents.providers.litellm_stream_types import _openai_tool_call
from app.services.mapping.itemized.provider_history import reasoning_projection_rows
from app.services.mapping.itemized.provider_request import (
    project_ai_message_content,
    project_user_message_content,
)


class LiteLLMHistoryProjectionMixin:
    def normalize_history_content(self, content: Any) -> Any:
        """把 checkpoint 历史消息转换为 LiteLLM/OpenAI-compatible 可接受内容。"""
        if not isinstance(content, list):
            return content

        normalized: list[dict[str, Any] | Any] = []
        changed = False
        for block in content:
            if isinstance(block, str):
                normalized.append({"type": "text", "text": block})
                changed = True
                continue
            if not isinstance(block, dict):
                normalized.append({"type": "text", "text": str(block)})
                changed = True
                continue

            block_type = block.get("type")
            # LangChain 合并流式 content block 时，重复 delta 可能把 type
            # 拼成 ``reasoningreasoningreasoning``。这些块仍然是内部思考，
            # 不能作为 Chat Completions 正文透传给不支持该类型的目标模型。
            if isinstance(block_type, str) and block_type.startswith(
                ("reasoning", "thinking", "redacted_thinking")
            ):
                changed = True
                continue
            if block_type == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    normalized.append({"type": "text", "text": text})
                changed = True
                continue
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    normalized.append({"type": "text", "text": text})
                    if set(block) != {"type", "text"}:
                        changed = True
                else:
                    changed = True
                continue

            fallback_text = block.get("text")
            if isinstance(fallback_text, str):
                normalized.append({"type": "text", "text": fallback_text})
                changed = True
                continue
            normalized.append(block)

        if not normalized:
            return ""
        if not changed:
            return content
        return normalized

    @staticmethod
    def normalize_output_content(content: Any) -> Any:
        """把 LiteLLM 输出中的 thinking/output_text 等方言转为标准块。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else ""
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]

        normalized: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    normalized.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                normalized.append({"type": "text", "text": str(block)})
                continue

            block_type = block.get("type")
            if block_type in {
                "reasoning",
                "reasoning_content",
                "reasoning_items",
                "thinking",
                "redacted_thinking",
            }:
                # LiteLLM 已经给出标准 block 时整体复制，不能在这里按字段
                # 白名单重建，否则 provider 新增的字段会在历史中丢失。
                normalized.append(copy.deepcopy(block))
                continue
            if block_type in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    normalized.append({"type": "text", "text": text})
                continue
            if block_type in {"tool_call", "tool_call_chunk"}:
                continue
            if isinstance(block_type, str):
                normalized.append(copy.deepcopy(block))
                continue

            fallback_text = block.get("text")
            if isinstance(fallback_text, str):
                normalized.append({"type": "text", "text": fallback_text})

        return normalized or ""

    def _normalize_history_content(self, content: Any) -> Any:
        return self.normalize_history_content(content)

    @staticmethod
    def _history_reasoning_content(content: Any) -> str | None:
        """从直接 reasoning block 提取 Chat Completions 所需的思考文本。"""
        parts = [
            str(row["text"])
            for row in reasoning_projection_rows(content)
            if row.get("kind") in {"reasoning", "summary"}
            and isinstance(row.get("text"), str)
            and row["text"]
        ]
        return "\n".join(parts) or None

    def _apply_reasoning_content_replay(
        self,
        message_dict: dict[str, Any],
        *,
        content: Any,
    ) -> None:
        if not self.reasoning_content_replay and not self.thinking_blocks_replay:
            message_dict.pop("reasoning_content", None)
            message_dict.pop("thinking_blocks", None)
            return
        target_capabilities: set[str] = set()
        if self.reasoning_content_replay:
            target_capabilities.add("reasoning_content_replay")
        if self.thinking_blocks_replay:
            target_capabilities.add("thinking_blocks")
        projection = project_ai_message_content(
            content,
            target_provider=self.provider_id,
            target_capabilities=target_capabilities,
        )
        if self.reasoning_content_replay:
            reasoning = projection.get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning:
                reasoning = self._history_reasoning_content(content)
            if reasoning:
                message_dict["reasoning_content"] = reasoning
        if self.thinking_blocks_replay and "thinking_blocks" in projection:
            message_dict["thinking_blocks"] = projection["thinking_blocks"]

    def _convert_messages_to_dicts(
        self, messages: Sequence[BaseMessage | dict[str, Any]]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, dict):
                item = dict(message)
                role = item.get("role")
                if role == "human":
                    item["role"] = "user"
                elif role == "ai":
                    item["role"] = "assistant"
                original_content = item.get("content")
                if item.get("role") == "user":
                    item.pop("response_metadata", None)
                    item.pop("additional_kwargs", None)
                    user_projection = project_user_message_content(
                        original_content,
                        target_format="chat_completions",
                        image_input=self.image_input_replay,
                    )
                    item["content"] = user_projection["content"]
                    result.append(item)
                    continue
                projection = project_ai_message_content(
                    original_content,
                    target_provider=self.provider_id,
                    target_capabilities=(
                        {
                            *(
                                {"reasoning_content_replay"}
                                if self.reasoning_content_replay
                                else set()
                            ),
                            *(
                                {"thinking_blocks"}
                                if self.thinking_blocks_replay
                                else set()
                            ),
                        }
                    ),
                )
                item["content"] = self.normalize_history_content(projection["content"])
                if item.get("role") == "assistant":
                    self._apply_reasoning_content_replay(
                        item,
                        content=original_content,
                    )
                result.append(item)
                continue

            if isinstance(message, HumanMessage):
                user_projection = project_user_message_content(
                    message.content,
                    target_format="chat_completions",
                    image_input=self.image_input_replay,
                )
                message_dict = {
                    "content": user_projection["content"],
                    "role": "user",
                }
                if message.name:
                    message_dict["name"] = message.name
                result.append(message_dict)
                continue

            projection = project_ai_message_content(
                message.content,
                target_provider=self.provider_id,
                target_capabilities=(
                    {
                        *(
                            {"reasoning_content_replay"}
                            if self.reasoning_content_replay
                            else set()
                        ),
                        *(
                            {"thinking_blocks"}
                            if self.thinking_blocks_replay
                            else set()
                        ),
                    }
                ),
                response_metadata=message.response_metadata,
            )
            message_dict: dict[str, Any] = {
                "content": self.normalize_history_content(projection["content"]),
            }
            if isinstance(message, ChatMessage):
                message_dict["role"] = message.role
            elif isinstance(message, HumanMessage):
                message_dict["role"] = "user"
            elif isinstance(message, AIMessage):
                message_dict["role"] = "assistant"
                if message.tool_calls:
                    message_dict["tool_calls"] = [
                        _openai_tool_call(tool_call) for tool_call in message.tool_calls
                    ]
                elif "tool_calls" in message.additional_kwargs:
                    message_dict["tool_calls"] = message.additional_kwargs["tool_calls"]
                if "function_call" in message.additional_kwargs:
                    message_dict["function_call"] = message.additional_kwargs[
                        "function_call"
                    ]
                self._apply_reasoning_content_replay(
                    message_dict,
                    content=message.content,
                )
            elif isinstance(message, SystemMessage):
                message_dict["role"] = "system"
            elif isinstance(message, FunctionMessage):
                message_dict["role"] = "function"
                message_dict["name"] = message.name
            elif isinstance(message, ToolMessage):
                message_dict["role"] = "tool"
                message_dict["tool_call_id"] = message.tool_call_id
                if message.name:
                    message_dict["name"] = message.name
            else:
                raise TypeError(
                    f"未知 LangChain message 类型: {type(message).__name__}"
                )

            if message.name and "name" not in message_dict:
                message_dict["name"] = message.name
            result.append(message_dict)
        return result
