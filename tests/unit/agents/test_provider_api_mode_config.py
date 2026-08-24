from __future__ import annotations

import pytest

from app.agents.provider_api_mode import (
    parse_provider_api_mode,
    provider_instance_id,
)


def _provider(api_mode: dict[str, object]) -> dict[str, object]:
    return {"id": "provider-test", "api_mode": api_mode}


def test_parses_chat_completions_reasoning_content_and_litellm_modalities():
    parsed = parse_provider_api_mode(
        _provider(
            {
                "protocol": "chat_completions",
                "model_info": {
                    "supports_vision": True,
                    "supports_audio_input": True,
                    "supports_function_calling": True,
                    "supports_reasoning": True,
                },
                "supports_reasoning": {"reasoning_content": True},
            }
        )
    )

    assert parsed.protocol == "chat_completions"
    assert parsed.model_info.supports_vision is True
    assert parsed.model_info.supports_audio_input is True
    assert parsed.supports_reasoning.reasoning_content is True
    assert parsed.request_features.prompt_cache_key is False


def test_parses_chat_completions_thinking_blocks_without_reasoning_content():
    parsed = parse_provider_api_mode(
        _provider(
            {
                "protocol": "chat_completions",
                "model_info": {
                    "supports_reasoning": True,
                    "supports_audio_output": True,
                },
                "supports_reasoning": {"thinking_blocks": True},
            }
        )
    )

    assert parsed.supports_reasoning.thinking_blocks is True
    assert parsed.model_info.supports_audio_output is True


def test_parses_responses_summary_and_same_source_encrypted_replay():
    parsed = parse_provider_api_mode(
        _provider(
            {
                "protocol": "responses",
                "model_info": {
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                },
                "supports_reasoning": {
                    "reasoning_items": {
                        "summary": True,
                        "encrypted_content": True,
                    }
                },
                "replay_policy": {"encrypted_content": "same_source"},
                "request_features": {"prompt_cache_key": True},
            }
        )
    )

    assert parsed.supports_reasoning.reasoning_items_summary is True
    assert parsed.supports_reasoning.reasoning_items_encrypted_content is True
    assert parsed.replay_policy.encrypted_content == "same_source"
    assert parsed.request_features.prompt_cache_key is True


def test_parses_anthropic_thinking_and_redacted_thinking_blocks():
    parsed = parse_provider_api_mode(
        _provider(
            {
                "protocol": "anthropic_messages",
                "model_info": {
                    "supports_reasoning": True,
                    "supports_vision": True,
                },
                "supports_reasoning": {
                    "thinking_blocks": {
                        "thinking": True,
                        "redacted_thinking": True,
                    }
                },
            }
        )
    )

    assert parsed.supports_reasoning.thinking_blocks_thinking is True
    assert parsed.supports_reasoning.thinking_blocks_redacted_thinking is True


def test_rejects_legacy_string_api_mode():
    with pytest.raises(TypeError, match="provider.api_mode 必须是对象"):
        parse_provider_api_mode(_provider("responses"))  # type: ignore[arg-type]


def test_rejects_mismatch_between_model_info_and_reasoning_fields():
    with pytest.raises(ValueError, match="supports_reasoning"):
        parse_provider_api_mode(
            _provider(
                {
                    "protocol": "chat_completions",
                    "model_info": {"supports_reasoning": False},
                    "supports_reasoning": {"reasoning_content": True},
                }
            )
        )


def test_encrypted_source_identity_uses_provider_instance_id():
    assert provider_instance_id(
        {
            "id": "backup_4",
            "custom_llm_provider": "chatgpt",
        }
    ) == "backup_4"
