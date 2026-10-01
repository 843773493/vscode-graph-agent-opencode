"""Provider API 协议、模型能力和历史消息回放策略的统一配置解析。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ApiProtocol = Literal["chat_completions", "responses", "anthropic_messages"]
EncryptedContentReplayPolicy = Literal["same_source"]

SUPPORTED_API_PROTOCOLS: frozenset[str] = frozenset(
    {"chat_completions", "responses", "anthropic_messages"}
)
_MODEL_INFO_FIELDS = frozenset(
    {
        "supports_vision",
        "supports_video_input",
        "supports_audio_input",
        "supports_audio_output",
        "supports_function_calling",
        "supports_reasoning",
    }
)
_REQUEST_FEATURE_FIELDS = frozenset({"prompt_cache_key"})
_REPLAY_POLICY_FIELDS = frozenset({"encrypted_content"})


@dataclass(frozen=True)
class ProviderModelInfo:
    """与 LiteLLM ModelInfo 对齐的模型能力声明。"""

    supports_vision: bool = False
    supports_video_input: bool = False
    supports_audio_input: bool = False
    supports_audio_output: bool = False
    supports_function_calling: bool = False
    supports_reasoning: bool = False


@dataclass(frozen=True)
class ProviderReasoningSupport:
    """按协议记录可接受的 LiteLLM reasoning 字段。"""

    reasoning_content: bool = False
    thinking_blocks: bool = False
    thinking_blocks_thinking: bool = False
    thinking_blocks_redacted_thinking: bool = False
    reasoning_items: bool = False
    reasoning_items_summary: bool = False
    reasoning_items_encrypted_content: bool = False


@dataclass(frozen=True)
class ProviderRequestFeatures:
    """不是模型能力、而是请求适配器可附加的请求特性。"""

    prompt_cache_key: bool = False


@dataclass(frozen=True)
class ProviderReplayPolicy:
    """历史思考块回放策略。当前密文只允许同一 provider 实例回放。"""

    encrypted_content: EncryptedContentReplayPolicy = "same_source"


@dataclass(frozen=True)
class ProviderApiMode:
    """一个 provider 的完整 API 模式配置。"""

    protocol: ApiProtocol
    model_info: ProviderModelInfo
    supports_reasoning: ProviderReasoningSupport
    request_features: ProviderRequestFeatures
    replay_policy: ProviderReplayPolicy


def _require_object(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} 必须是对象")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} 必须是布尔值")
    return value


def _reject_unknown_fields(
    value: Mapping[str, object],
    allowed: frozenset[str],
    field_name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{field_name} 包含不支持的字段: {', '.join(str(item) for item in unknown)}"
        )


def _parse_model_info(raw: object) -> ProviderModelInfo:
    value = _require_object(raw, "provider.api_mode.model_info")
    _reject_unknown_fields(value, _MODEL_INFO_FIELDS, "provider.api_mode.model_info")
    defaults = {
        field_name: False
        for field_name in _MODEL_INFO_FIELDS
    }
    parsed = {
        field_name: _require_bool(
            value.get(field_name, default),
            f"provider.api_mode.model_info.{field_name}",
        )
        for field_name, default in defaults.items()
    }
    return ProviderModelInfo(**parsed)


def _parse_reasoning_support(
    protocol: ApiProtocol,
    raw: object,
) -> ProviderReasoningSupport:
    value = _require_object(raw, "provider.api_mode.supports_reasoning")
    if protocol == "chat_completions":
        _reject_unknown_fields(
            value,
            frozenset({"reasoning_content", "thinking_blocks"}),
            "provider.api_mode.supports_reasoning",
        )
        return ProviderReasoningSupport(
            reasoning_content=_require_bool(
                value.get("reasoning_content", False),
                "provider.api_mode.supports_reasoning.reasoning_content",
            ),
            thinking_blocks=_require_bool(
                value.get("thinking_blocks", False),
                "provider.api_mode.supports_reasoning.thinking_blocks",
            ),
        )

    if protocol == "responses":
        _reject_unknown_fields(
            value,
            frozenset({"reasoning_items"}),
            "provider.api_mode.supports_reasoning",
        )
        items = _require_object(
            value.get("reasoning_items", {}),
            "provider.api_mode.supports_reasoning.reasoning_items",
        )
        _reject_unknown_fields(
            items,
            frozenset({"summary", "encrypted_content"}),
            "provider.api_mode.supports_reasoning.reasoning_items",
        )
        return ProviderReasoningSupport(
            reasoning_items=True,
            reasoning_items_summary=_require_bool(
                items.get("summary", False),
                "provider.api_mode.supports_reasoning.reasoning_items.summary",
            ),
            reasoning_items_encrypted_content=_require_bool(
                items.get("encrypted_content", False),
                "provider.api_mode.supports_reasoning.reasoning_items.encrypted_content",
            ),
        )

    _reject_unknown_fields(
        value,
        frozenset({"thinking_blocks"}),
        "provider.api_mode.supports_reasoning",
    )
    blocks = _require_object(
        value.get("thinking_blocks", {}),
        "provider.api_mode.supports_reasoning.thinking_blocks",
    )
    _reject_unknown_fields(
        blocks,
        frozenset({"thinking", "redacted_thinking"}),
        "provider.api_mode.supports_reasoning.thinking_blocks",
    )
    return ProviderReasoningSupport(
        thinking_blocks=True,
        thinking_blocks_thinking=_require_bool(
            blocks.get("thinking", False),
            "provider.api_mode.supports_reasoning.thinking_blocks.thinking",
        ),
        thinking_blocks_redacted_thinking=_require_bool(
            blocks.get("redacted_thinking", False),
            "provider.api_mode.supports_reasoning.thinking_blocks.redacted_thinking",
        ),
    )


def _parse_request_features(raw: object) -> ProviderRequestFeatures:
    value = _require_object(raw, "provider.api_mode.request_features")
    _reject_unknown_fields(
        value,
        _REQUEST_FEATURE_FIELDS,
        "provider.api_mode.request_features",
    )
    return ProviderRequestFeatures(
        prompt_cache_key=_require_bool(
            value.get("prompt_cache_key", False),
            "provider.api_mode.request_features.prompt_cache_key",
        )
    )


def _parse_replay_policy(raw: object) -> ProviderReplayPolicy:
    value = _require_object(raw, "provider.api_mode.replay_policy")
    _reject_unknown_fields(
        value,
        _REPLAY_POLICY_FIELDS,
        "provider.api_mode.replay_policy",
    )
    encrypted_content = value.get("encrypted_content", "same_source")
    if encrypted_content != "same_source":
        raise ValueError(
            "provider.api_mode.replay_policy.encrypted_content 当前只支持 'same_source'"
        )
    return ProviderReplayPolicy(encrypted_content="same_source")


def parse_provider_api_mode(provider: Mapping[str, object]) -> ProviderApiMode:
    """严格解析 provider.api_mode，拒绝旧的字符串模式和未知字段。"""
    raw = _require_object(provider.get("api_mode"), "provider.api_mode")
    protocol = raw.get("protocol")
    if protocol not in SUPPORTED_API_PROTOCOLS:
        supported = ", ".join(sorted(SUPPORTED_API_PROTOCOLS))
        raise ValueError(
            f"provider.api_mode.protocol 不受支持: {protocol!r}；允许值: {supported}"
        )

    model_info = _parse_model_info(raw.get("model_info", {}))
    reasoning = _parse_reasoning_support(protocol, raw.get("supports_reasoning", {}))
    if model_info.supports_reasoning != any(
        (
            reasoning.reasoning_content,
            reasoning.thinking_blocks,
            reasoning.thinking_blocks_thinking,
            reasoning.thinking_blocks_redacted_thinking,
            reasoning.reasoning_items_summary,
            reasoning.reasoning_items_encrypted_content,
        )
    ):
        raise ValueError(
            "provider.api_mode.model_info.supports_reasoning 必须与 "
            "provider.api_mode.supports_reasoning 中的实际字段一致"
        )

    _reject_unknown_fields(
        raw,
        frozenset(
            {
                "protocol",
                "model_info",
                "supports_reasoning",
                "request_features",
                "replay_policy",
            }
        ),
        "provider.api_mode",
    )
    return ProviderApiMode(
        protocol=protocol,
        model_info=model_info,
        supports_reasoning=reasoning,
        request_features=_parse_request_features(raw.get("request_features", {})),
        replay_policy=_parse_replay_policy(raw.get("replay_policy", {})),
    )


def provider_instance_id(provider: Mapping[str, object]) -> str | None:
    """返回用于密文回放绑定的应用 provider 实例 ID，而不是 custom_llm_provider。"""
    value = provider.get("id")
    return value if isinstance(value, str) and value else None
