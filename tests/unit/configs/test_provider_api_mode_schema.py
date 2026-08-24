from __future__ import annotations

from pathlib import Path

import commentjson
import jsonschema
import pytest


def _provider_schema() -> dict[str, object]:
    schema = commentjson.loads(
        Path("configs/workspace_schema.jsonc").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema["$defs"]["provider"]


def _provider(api_mode: dict[str, object]) -> dict[str, object]:
    return {
        "id": "schema-provider",
        "endpoint": "https://example.com/v1",
        "model": "schema-model",
        "api_key": "test-key",
        "custom_llm_provider": "openai",
        "api_mode": api_mode,
    }


def test_schema_accepts_chat_completions_model_info_and_reasoning_content():
    jsonschema.validate(
        _provider(
            {
                "protocol": "chat_completions",
                "model_info": {
                    "supports_vision": True,
                    "supports_audio_input": True,
                    "supports_reasoning": True,
                },
                "supports_reasoning": {"reasoning_content": True},
            }
        ),
        _provider_schema(),
    )


def test_schema_accepts_chat_completions_thinking_blocks():
    jsonschema.validate(
        _provider(
            {
                "protocol": "chat_completions",
                "model_info": {"supports_reasoning": True},
                "supports_reasoning": {"thinking_blocks": True},
            }
        ),
        _provider_schema(),
    )


def test_schema_accepts_responses_reasoning_items_and_replay_policy():
    jsonschema.validate(
        _provider(
            {
                "protocol": "responses",
                "model_info": {
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                },
                "supports_reasoning": {
                    "reasoning_items": {"summary": True, "encrypted_content": True}
                },
                "replay_policy": {"encrypted_content": "same_source"},
            }
        ),
        _provider_schema(),
    )


def test_schema_accepts_anthropic_thinking_block_shape():
    provider = _provider(
        {
            "protocol": "anthropic_messages",
            "model_info": {"supports_reasoning": True},
            "supports_reasoning": {
                "thinking_blocks": {
                    "thinking": True,
                    "redacted_thinking": True,
                }
            },
        }
    )
    provider["custom_llm_provider"] = "anthropic"
    jsonschema.validate(
        provider,
        _provider_schema(),
    )


def test_schema_rejects_legacy_string_api_mode_and_flat_capabilities():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                **_provider(
                    {
                        "protocol": "chat_completions",
                        "model_info": {},
                        "supports_reasoning": {"reasoning_content": False},
                    }
                ),
                "api_mode": "responses",
            },
            _provider_schema(),
        )

    legacy = _provider(
        {
            "protocol": "chat_completions",
            "model_info": {},
            "supports_reasoning": {"reasoning_content": False},
        }
    )
    legacy["capabilities"] = ["image_input"]
    with pytest.raises(jsonschema.ValidationError, match="capabilities"):
        jsonschema.validate(legacy, _provider_schema())


def test_schema_rejects_unknown_model_info_and_replay_policy_fields():
    with pytest.raises(jsonschema.ValidationError, match="unknown"):
        jsonschema.validate(
            _provider(
                {
                    "protocol": "chat_completions",
                    "model_info": {"unknown": True},
                    "supports_reasoning": {"reasoning_content": False},
                }
            ),
            _provider_schema(),
        )


def test_schema_requires_anthropic_custom_provider_for_anthropic_protocol():
    provider = _provider(
        {
            "protocol": "anthropic_messages",
            "model_info": {},
            "supports_reasoning": {"thinking_blocks": False},
        }
    )
    with pytest.raises(jsonschema.ValidationError, match="anthropic"):
        jsonschema.validate(provider, _provider_schema())


def test_schema_rejects_protocol_specific_reasoning_shape_mismatch():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            _provider(
                {
                    "protocol": "chat_completions",
                    "model_info": {"supports_reasoning": True},
                    "supports_reasoning": {
                        "thinking_blocks": {
                            "thinking": True,
                            "redacted_thinking": True,
                        }
                    },
                }
            ),
            _provider_schema(),
        )

    with pytest.raises(jsonschema.ValidationError, match="different_source"):
        jsonschema.validate(
            _provider(
                {
                    "protocol": "responses",
                    "model_info": {"supports_reasoning": False},
                    "supports_reasoning": {
                        "reasoning_items": {"summary": False, "encrypted_content": False}
                    },
                    "replay_policy": {"encrypted_content": "different_source"},
                }
            ),
            _provider_schema(),
        )
