"""真实 Provider wire 的唯一纯 finalize 转换与 profile/params 提取。

本模块是请求编码面的唯一 owner：Chat Completions 与 Responses 的最终发送体、
profile 身份和可哈希参数只在这里构造一次。它不读取 storage、不导入动态模型或
容器，也不实现 ToolSet 投影；调用方（Saver 编排与 provider adapter）必须传入
同一份 sealed tools 与已冻结参数，保证封存与发送使用同一转换。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    project_tool_message_content,
    project_user_message_content,
)

# 认证与本地传输只属于连接本身，不进入 request hash；其余真实语义参数
# （含 model_kwargs 展开的 temperature/max_tokens/extra_body 等）必须参与。
WIRE_EXCLUDED_KEYS = frozenset(
    {
        "api_key",
        "api_base",
        "organization",
        "extra_headers",
        "timeout",
        # LiteLLM 内部路由键：真实 HTTP body 中不存在，归 profile，不进 wire。
        "custom_llm_provider",
        "base_model",
    }
)

# 封存正文的 wire 字段只能来自 Saver 已投影结果；模型实例参数携带这些字段
# 必须显式拒绝，避免出现“发送一份、哈希另一份”。
CHAT_SEALED_KEYS = frozenset({"messages", "tools"})
# Responses 边界与迁移前 native 绑定校验（已物理下线）的 forbidden 集合逐字
# 一致：input/tools/include/store 由封存投影显式传入并按 §2.2 覆盖参数
# （native 覆盖 parameters），因此不能拒绝 LangChain bind_tools 经 kwargs
# 转发的 tools；其余指令位置仍必须拒绝，不能放宽。
RESPONSES_SEALED_KEYS = frozenset({
    "input", "messages", "instructions", "previous_response_id", "prompt",
})


@dataclass(frozen=True, slots=True)
class ChatWireProfile:
    """Chat Completions 请求的能力与身份事实；由模型实例冻结后进入 provider_version。"""

    provider_id: str | None
    custom_llm_provider: str
    model: str
    reasoning_content_replay: bool = False
    thinking_blocks_replay: bool = False
    image_input_replay: bool = False

    def capabilities(self) -> dict[str, bool]:
        return {
            "reasoning_content_replay": self.reasoning_content_replay,
            "thinking_blocks_replay": self.thinking_blocks_replay,
            "image_input_replay": self.image_input_replay,
        }

    def identity(self) -> str:
        return wire_profile_identity(
            protocol="chat_completions",
            provider_id=self.provider_id,
            custom_llm_provider=self.custom_llm_provider,
            model=self.model,
            capabilities=self.capabilities(),
        )


@dataclass(frozen=True, slots=True)
class ResponsesWireProfile:
    """Responses API 请求的能力与身份事实；include/store 是真实 payload 语义。"""

    provider_id: str | None
    custom_llm_provider: str
    model: str
    include: tuple[str, ...] = ()
    store: bool = False
    reasoning_items_summary_replay: bool = False
    reasoning_items_encrypted_replay: bool = False
    image_input_replay: bool = False

    def capabilities(self) -> dict[str, bool]:
        return {
            "store": self.store,
            "reasoning_items_summary_replay": self.reasoning_items_summary_replay,
            "reasoning_items_encrypted_replay": self.reasoning_items_encrypted_replay,
            "image_input_replay": self.image_input_replay,
            **{f"include:{value}": True for value in self.include},
        }

    def identity(self) -> str:
        return wire_profile_identity(
            protocol="responses",
            provider_id=self.provider_id,
            custom_llm_provider=self.custom_llm_provider,
            model=self.model,
            capabilities=self.capabilities(),
        )


def wire_profile_identity(
    *,
    protocol: str,
    provider_id: str | None,
    custom_llm_provider: str,
    model: str,
    capabilities: Mapping[str, bool],
) -> str:
    """profile 字符串：只标识 provider/model 能力身份，不代表投影算法版本。"""
    if not protocol or not model:
        raise ValueError("provider wire profile 缺少 protocol/model")
    flags = ",".join(
        f"{name}=1" for name, enabled in sorted(capabilities.items()) if enabled
    )
    return (
        f"{protocol}|"
        f"provider={provider_id or ''}|custom={custom_llm_provider}"
        f"|model={model}|caps={flags}"
    )


def provider_wire_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """从实际发送参数归一：None 省略，认证/传输键剔除，其余真实语义参数保留。"""
    return {
        key: value
        for key, value in params.items()
        if value is not None and key not in WIRE_EXCLUDED_KEYS
    }


def _reject_sealed_overrides(params: Mapping[str, Any], forbidden: frozenset[str], *, protocol: str) -> None:
    """参数不得覆盖封存正文；出现即网络前失败，不能只哈希另一份正文。"""
    overridden = sorted(key for key in params if key in forbidden)
    if overridden:
        raise ValueError(
            f"""source-mismatch: {protocol} provider 参数覆盖 sealed context: """
            + ",".join(overridden)
        )


def finalize_chat_wire(
    *,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Chat Completions 最终 wire：真实消息、sealed tools 与剩余请求参数。"""
    _reject_sealed_overrides(params, CHAT_SEALED_KEYS, protocol="Chat Completions")
    payload: dict[str, Any] = {"messages": [dict(message) for message in messages]}
    if tools is not None:
        payload["tools"] = [dict(tool) for tool in tools]
    payload.update(params)
    return payload


def strip_responses_reasoning_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    """移除 Responses reasoning 输入项的输出态 content。

    封存与发送共用这一份最终边界清洗，adapter 不再各自复制。
    TODO: 等所有兼容 Responses provider 支持 reasoning.content 的历史输入后，
    再删除这个最终边界清洗。
    """
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return dict(payload)
    cleaned: list[Any] = []
    changed = False
    for item in input_items:
        if (
            isinstance(item, dict)
            and item.get("type") == "reasoning"
            and "content" in item
        ):
            cleaned.append(
                {key: value for key, value in item.items() if key != "content"}
            )
            changed = True
            continue
        cleaned.append(item)
    if not changed:
        return dict(payload)
    return {**payload, "input": cleaned}


def merge_system_messages_into_instructions(payload: Mapping[str, Any]) -> dict[str, Any]:
    """ChatGPT Codex endpoint 只接受 instructions，不接受 system input。

    TODO: LiteLLM 的 chatgpt provider 原生把 system/developer input 合并进
    instructions 后，删除此兼容转换。
    """
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return dict(payload)

    instruction_parts: list[str] = []
    remaining_items: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("role") not in {
            "system",
            "developer",
        }:
            remaining_items.append(item)
            continue

        content = item.get("content")
        if isinstance(content, str):
            instruction_parts.append(content)
            continue
        if not isinstance(content, list):
            raise TypeError(
                "ChatGPT system message content 必须是字符串或内容块列表"
            )
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {
                "text",
                "input_text",
            }:
                raise TypeError("ChatGPT system message 只支持文本内容块")
            text = block.get("text")
            if not isinstance(text, str):
                raise TypeError("ChatGPT system message 文本块缺少 text 字符串")
            instruction_parts.append(text)

    if not instruction_parts:
        return dict(payload)
    existing_instructions = payload.get("instructions")
    if existing_instructions is not None:
        if not isinstance(existing_instructions, str):
            raise TypeError("Responses instructions 必须是字符串")
        instruction_parts.insert(0, existing_instructions)

    return {
        **payload,
        "input": remaining_items,
        "instructions": "\n\n".join(instruction_parts),
    }


def finalize_responses_body(
    payload: Mapping[str, Any],
    *,
    merge_system_to_instructions: bool = False,
) -> dict[str, Any]:
    """Responses 最终正文清洗：唯一纯转换，封存与发送共用。"""
    body = strip_responses_reasoning_output(payload)
    if merge_system_to_instructions:
        body = merge_system_messages_into_instructions(body)
    return body


def finalize_responses_wire(
    *,
    input_items: Sequence[object],
    tools: Sequence[Mapping[str, Any]] | None,
    params: Mapping[str, Any],
    include: Sequence[str],
    store: bool,
    instructions: str | None = None,
    merge_system_to_instructions: bool = False,
) -> dict[str, Any]:
    """Responses 最终 wire：input/tools/include/store 与真实语义参数合并。

    最终正文清洗只在这里实现一次：reasoning 输出态 content 与 chatgpt 的
    system/developer→instructions 属于同一转换，adapter 不得再复制一份。
    """
    _reject_sealed_overrides(params, RESPONSES_SEALED_KEYS, protocol="Responses")
    payload: dict[str, Any] = {
        **params,
        "input": list(input_items),
        "include": list(include),
        "store": store,
    }
    if tools is not None:
        payload["tools"] = [dict(tool) for tool in tools]
    if instructions is not None:
        payload["instructions"] = instructions
    return finalize_responses_body(
        payload,
        merge_system_to_instructions=merge_system_to_instructions,
    )


def normalize_chat_history_content(content: Any) -> Any:
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
    message_dict: dict[str, Any],
    *,
    content: Any,
    profile: ChatWireProfile,
) -> None:
    if not profile.reasoning_content_replay and not profile.thinking_blocks_replay:
        message_dict.pop("reasoning_content", None)
        message_dict.pop("thinking_blocks", None)
        return
    target_capabilities: set[str] = set()
    if profile.reasoning_content_replay:
        target_capabilities.add("reasoning_content_replay")
    if profile.thinking_blocks_replay:
        target_capabilities.add("thinking_blocks")
    projection = project_ai_message_content(
        content,
        target_provider=profile.provider_id,
        target_capabilities=target_capabilities,
    )
    if profile.reasoning_content_replay:
        reasoning = projection.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            reasoning = _history_reasoning_content(content)
        if reasoning:
            message_dict["reasoning_content"] = reasoning
    if profile.thinking_blocks_replay and "thinking_blocks" in projection:
        message_dict["thinking_blocks"] = projection["thinking_blocks"]


def _tool_result_path(message: ToolMessage) -> str | None:
    """读取工具结果记录的文件路径，用于降级占位符提示。"""
    additional_kwargs = message.additional_kwargs or {}
    path = additional_kwargs.get("read_file_path")
    return path if isinstance(path, str) and path else None



def chat_wire_profile(model: object) -> ChatWireProfile:
    """从模型实例只读提取 Chat profile；不持有实例引用、不触发网络。"""
    return ChatWireProfile(
        provider_id=getattr(model, "provider_id", None),
        custom_llm_provider=str(getattr(model, "custom_llm_provider", "") or ""),
        model=str(getattr(model, "model_name", None) or getattr(model, "model", "") or ""),
        reasoning_content_replay=bool(getattr(model, "reasoning_content_replay", False)),
        thinking_blocks_replay=bool(getattr(model, "thinking_blocks_replay", False)),
        image_input_replay=bool(getattr(model, "image_input_replay", False)),
    )


def responses_wire_profile(model: object) -> ResponsesWireProfile:
    """从模型实例只读提取 Responses profile；capability 漂移必须改变身份。"""
    include = getattr(model, "responses_include", ()) or ()
    if not isinstance(include, (list, tuple)) or not all(
        isinstance(value, str) for value in include
    ):
        raise TypeError("Responses include 必须是字符串序列")
    return ResponsesWireProfile(
        provider_id=getattr(model, "provider_id", None),
        custom_llm_provider=str(getattr(model, "custom_llm_provider", "") or ""),
        model=str(getattr(model, "model_name", None) or getattr(model, "model", "") or ""),
        include=tuple(include),
        store=bool(getattr(model, "responses_store", False)),
        reasoning_items_summary_replay=bool(
            getattr(model, "reasoning_items_summary_replay", False)
        ),
        reasoning_items_encrypted_replay=bool(
            getattr(model, "reasoning_items_encrypted_replay", False)
        ),
        image_input_replay=bool(getattr(model, "image_input_replay", False)),
    )


def _client_params(model: object) -> Mapping[str, Any]:
    params = getattr(model, "_client_params", None)
    if not isinstance(params, Mapping):
        raise TypeError("provider 实例缺少 _client_params 只读视图")
    return params


def chat_send_params(
    model: object,
    *,
    stop: Sequence[str] | None = None,
    kwargs: Mapping[str, Any] | None = None,
    stream: bool | None = None,
) -> dict[str, Any]:
    """Chat adapter 的真实发送参数：保留凭据，行为与迁移前逐字段一致。

    ``_client_params`` 中的 None 与迁移前一样先省略，再逐字合并 ``kwargs``；
    ``stream`` 只由真实调用方传入（流式路径 True，非流式路径 None）。只有流式
    才带 ``stream_options``，非流式请求不得凭空获得 usage chunk 参数。
    凭据/传输键必须保留给 LiteLLM 建连。
    """
    params = {
        key: value for key, value in _client_params(model).items()
        if value is not None
    }
    if stop is not None:
        if "stop" in params:
            raise ValueError("`stop` 同时出现在输入和默认参数中")
        params["stop"] = list(stop)
    if kwargs:
        params.update(kwargs)
    if stream is not None:
        params["stream"] = bool(stream)
        if stream:
            stream_options = getattr(model, "stream_options", None)
            params["stream_options"] = (
                dict(stream_options) if stream_options else {"include_usage": True}
            )
        else:
            params.pop("stream_options", None)
    return params


def wire_request_params(
    model: object,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """把 adapter 发送参数归一成真实 HTTP body 的语义视图。

    本地回环实测（litellm-http-body-probe）：LiteLLM 在真实请求里 ① 去掉
    "<custom_llm_provider>/" 的 model 前缀；② 把 ``extra_body`` 展开为顶层键；
    ③ 丢弃 ``api_key``/``api_base``/``organization``/``extra_headers``/``timeout``
    与路由键 ``custom_llm_provider``/``base_model``；④ 丢弃 None 值。封存 preimage
    必须与真实 body 同形，否则发送侧与封存侧会出现第二份事实。
    """
    merged = {
        key: value for key, value in params.items() if key != "extra_body"
    }
    extra_body = params.get("extra_body")
    if extra_body is not None:
        if not isinstance(extra_body, Mapping):
            raise TypeError("provider extra_body 必须是对象")
        merged.update(extra_body)
    merged["model"] = _effective_model(model, params)
    return provider_wire_params(merged)


def _effective_model(model: object, params: Mapping[str, Any]) -> str:
    """LiteLLM 真实 body 中的 model：custom_llm_provider 前缀会被剥离。"""
    name = str(params.get("model") or getattr(model, "model", "") or "")
    custom = str(getattr(model, "custom_llm_provider", "") or "")
    if custom and name.startswith(custom + "/"):
        return name[len(custom) + 1 :]
    return name


def chat_wire_params(
    model: object,
    *,
    stop: Sequence[str] | None = None,
    kwargs: Mapping[str, Any] | None = None,
    stream: bool | None = None,
) -> dict[str, Any]:
    """Chat 封存参数视图：真实发送参数归一成 HTTP body 语义后再进 request hash。"""
    params = wire_request_params(
        model, chat_send_params(model, stop=stop, kwargs=kwargs, stream=stream)
    )
    _reject_sealed_overrides(params, CHAT_SEALED_KEYS, protocol="Chat Completions")
    return params


def responses_send_params(
    model: object,
    *,
    kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Responses adapter 的真实发送参数：保留凭据，与迁移前逐字段一致。"""
    params = {
        key: value for key, value in _client_params(model).items()
        if value is not None
    }
    if kwargs:
        params.update(kwargs)
    return params


def responses_wire_params(
    model: object,
    *,
    kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Responses 封存参数视图：真实发送参数归一成 HTTP body 语义后再进 request hash。"""
    params = wire_request_params(
        model, responses_send_params(model, kwargs=kwargs)
    )
    _reject_sealed_overrides(params, RESPONSES_SEALED_KEYS, protocol="Responses")
    return params

def finalize_chat_messages(
    messages: Sequence[BaseMessage | dict[str, Any]],
    *,
    profile: ChatWireProfile,
) -> list[dict[str, Any]]:
    """Chat Completions 真实消息编码；封存与发送共用这一份实现。"""
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
                    image_input=profile.image_input_replay,
                )
                item["content"] = user_projection["content"]
                result.append(item)
                continue
            projection = project_ai_message_content(
                original_content,
                target_provider=profile.provider_id,
                target_capabilities=(
                    {
                        *(
                            {"reasoning_content_replay"}
                            if profile.reasoning_content_replay
                            else set()
                        ),
                        *(
                            {"thinking_blocks"}
                            if profile.thinking_blocks_replay
                            else set()
                        ),
                    }
                ),
            )
            item["content"] = normalize_chat_history_content(projection["content"])
            if item.get("role") == "assistant":
                _apply_reasoning_content_replay(
                    item, content=original_content, profile=profile,
                )
            result.append(item)
            continue

        if isinstance(message, HumanMessage):
            user_projection = project_user_message_content(
                message.content,
                target_format="chat_completions",
                image_input=profile.image_input_replay,
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
            target_provider=profile.provider_id,
            target_capabilities=(
                {
                    *(
                        {"reasoning_content_replay"}
                        if profile.reasoning_content_replay
                        else set()
                    ),
                    *(
                        {"thinking_blocks"}
                        if profile.thinking_blocks_replay
                        else set()
                    ),
                }
            ),
            response_metadata=message.response_metadata,
        )
        message_dict: dict[str, Any] = {
            "content": normalize_chat_history_content(projection["content"]),
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
            _apply_reasoning_content_replay(
                message_dict, content=message.content, profile=profile,
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
            tool_projection = project_tool_message_content(
                message_dict["content"],
                image_input=profile.image_input_replay,
                path=_tool_result_path(message),
            )
            message_dict["content"] = tool_projection["content"]
        else:
            raise TypeError(
                f"未知 LangChain message 类型: {type(message).__name__}"
            )

        if message.name and "name" not in message_dict:
            message_dict["name"] = message.name
        result.append(message_dict)
    return result


__all__ = [
    "CHAT_SEALED_KEYS",
    "RESPONSES_SEALED_KEYS",
    "WIRE_EXCLUDED_KEYS",
    "ChatWireProfile",
    "ResponsesWireProfile",
    "chat_send_params",
    "chat_wire_params",
    "chat_wire_profile",
    "finalize_chat_messages",
    "finalize_chat_wire",
    "finalize_responses_body",
    "finalize_responses_wire",
    "merge_system_messages_into_instructions",
    "normalize_chat_history_content",
    "provider_wire_params",
    "responses_send_params",
    "responses_wire_params",
    "responses_wire_profile",
    "strip_responses_reasoning_output",
    "wire_profile_identity",
    "wire_request_params",
]
