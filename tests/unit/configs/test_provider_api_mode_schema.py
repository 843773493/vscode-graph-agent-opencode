from __future__ import annotations

from pathlib import Path

import commentjson
import jsonschema
import pytest


@pytest.fixture(scope="module")
def options_validator() -> jsonschema.Draft202012Validator:
    schema = commentjson.loads(
        Path("configs/workspace_schema.jsonc").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator({"$ref": "#/$defs/modelOptions", "$defs": schema["$defs"]})


def _validate_options(
    options_validator: jsonschema.Draft202012Validator,
    provider: dict[str, object],
) -> None:
    error = jsonschema.exceptions.best_match(options_validator.iter_errors(provider))
    if error is not None:
        raise error


def _options(api_mode: dict[str, object]) -> dict[str, object]:
    return {
        "endpoint": "https://example.com/v1", "api_key": "${SCHEMA_TEST_KEY}",
        "custom_llm_provider": "openai", "api_mode": api_mode,
    }


def test_provider_connection_can_be_saved_before_adding_models(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    validator = jsonschema.Draft202012Validator({
        "$ref": "#/$defs/provider", "$defs": options_validator.schema["$defs"],
    })
    provider = {"config_ref": "#/profiles/connection", "models": {}}
    validator.validate(provider)
    provider["models"] = {"broken": {"model": "", "config_ref": "context:provider/primary"}}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(provider)
    provider["models"] = {}
    provider["defaults"] = {}
    with pytest.raises(jsonschema.ValidationError, match="defaults"):
        validator.validate(provider)


def test_schema_accepts_chat_completions_model_info_and_reasoning_content(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    options_validator.validate(
        _options(
            {
                "protocol": "chat_completions",
                "model_info": {
                    "supports_vision": True,
                    "supports_audio_input": True,
                    "supports_reasoning": True,
                    "max_input_tokens": 400000,
                    "context_window": 128000,
                },
                "supports_reasoning": {"reasoning_content": True},
            }
        )
    )


def test_schema_accepts_chat_completions_thinking_blocks(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    options_validator.validate(
        _options(
            {
                "protocol": "chat_completions",
                "model_info": {"supports_reasoning": True},
                "supports_reasoning": {"thinking_blocks": True},
            }
        )
    )


def test_schema_accepts_responses_reasoning_items_and_replay_policy(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    options_validator.validate(
        _options(
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
        )
    )


def test_schema_accepts_anthropic_thinking_block_shape(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    provider = _options(
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
    options_validator.validate(provider)


def test_schema_rejects_legacy_string_api_mode_and_flat_capabilities(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    legacy_mode = _options({"protocol": "chat_completions"})
    legacy_mode["api_mode"] = "responses"
    with pytest.raises(jsonschema.ValidationError):
        options_validator.validate(legacy_mode)

    legacy = _options(
        {
            "protocol": "chat_completions",
            "model_info": {},
            "supports_reasoning": {"reasoning_content": False},
        }
    )
    legacy["capabilities"] = ["image_input"]
    with pytest.raises(jsonschema.ValidationError, match="capabilities"):
        _validate_options(options_validator, legacy)


def test_schema_rejects_unknown_model_info_and_replay_policy_fields(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    with pytest.raises(jsonschema.ValidationError, match="unknown"):
        _validate_options(
            options_validator,
            _options(
                {
                    "protocol": "chat_completions",
                    "model_info": {"unknown": True},
                    "supports_reasoning": {"reasoning_content": False},
                }
            ),
        )


def test_provider_fragment_schema_allows_model_level_protocol_override(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    provider = _options(
        {
            "protocol": "chat_completions",
            "model_info": {"supports_reasoning": True},
            "supports_reasoning": {"reasoning_content": True},
        }
    )
    provider.update({
        "custom_llm_provider": "anthropic",
        "api_mode": {"protocol": "anthropic_messages", "model_info": {"supports_reasoning": True},
                     "supports_reasoning": {"thinking_blocks": {"thinking": True, "redacted_thinking": False}}},
    })
    options_validator.validate(provider)


def test_provider_fragment_schema_defers_reasoning_protocol_validation(
    options_validator: jsonschema.Draft202012Validator,
) -> None:
    options_validator.validate(
        _options(
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
        )
    )

    with pytest.raises(jsonschema.ValidationError, match="different_source"):
        _validate_options(
            options_validator,
            _options(
                {
                    "protocol": "responses",
                    "model_info": {"supports_reasoning": False},
                    "supports_reasoning": {
                        "reasoning_items": {
                            "summary": False,
                            "encrypted_content": False,
                        }
                    },
                    "replay_policy": {"encrypted_content": "different_source"},
                }
            ),
        )
