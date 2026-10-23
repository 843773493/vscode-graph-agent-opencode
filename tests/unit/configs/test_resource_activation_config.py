"""context.resource_activation 配置族focused测试。"""

from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from configs.runtime import merge_json_objects, read_jsonc_object


@pytest.fixture(scope="module")
def workspace_config_validator() -> jsonschema.Draft202012Validator:
    schema = read_jsonc_object(Path("configs/workspace_schema.jsonc"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.fixture(scope="module")
def inline_workspace_config() -> dict[str, object]:
    return read_jsonc_object(Path("configs/workspace_inline.jsonc"))


@pytest.fixture(scope="module")
def development_workspace_config() -> dict[str, object]:
    return read_jsonc_object(Path("configs/workspace_dev.jsonc"))


def test_inline_default_and_dev_template_share_activation_contract(
    workspace_config_validator: jsonschema.Draft202012Validator,
    inline_workspace_config: dict[str, object],
    development_workspace_config: dict[str, object],
) -> None:
    workspace_config_validator.validate(inline_workspace_config)
    workspace_config_validator.validate(development_workspace_config)
    expected = {
        "default_boundary": "turn",
        "overrides": {"mcp_tool_catalog": "turn"},
    }
    assert inline_workspace_config["context"]["resource_activation"] == expected
    assert development_workspace_config["context"]["resource_activation"] == expected


def test_override_recursively_merges_and_schema_rejects_unknown_boundary(
    workspace_config_validator: jsonschema.Draft202012Validator,
    inline_workspace_config: dict[str, object],
) -> None:
    merged = merge_json_objects(
        inline_workspace_config,
        {
            "context": {
                "resource_activation": {
                    "default_boundary": "model_call",
                }
            }
        },
    )
    assert merged["context"]["resource_activation"] == {
        "default_boundary": "model_call",
        "overrides": {"mcp_tool_catalog": "turn"},
    }
    workspace_config_validator.validate(merged)
    invalid = merge_json_objects(
        inline_workspace_config,
        {
            "context": {
                "resource_activation": {
                    "default_boundary": "request_count",
                }
            }
        },
    )
    with pytest.raises(jsonschema.ValidationError):
        workspace_config_validator.validate(invalid)
