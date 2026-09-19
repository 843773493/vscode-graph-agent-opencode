"""context.resource_activation 配置族focused测试。"""

from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from configs.runtime import merge_json_objects, read_jsonc_object


def test_inline_default_and_dev_template_share_activation_contract() -> None:
    inline = read_jsonc_object(Path("configs/workspace_inline.jsonc"))
    dev = read_jsonc_object(Path("configs/workspace_dev.jsonc"))
    schema = read_jsonc_object(Path("configs/workspace_schema.jsonc"))
    jsonschema.validate(inline, schema)
    jsonschema.validate(dev, schema)
    expected = {
        "default_boundary": "turn",
        "overrides": {"mcp_tool_catalog": "turn"},
    }
    assert inline["context"]["resource_activation"] == expected
    assert dev["context"]["resource_activation"] == expected


def test_override_recursively_merges_and_schema_rejects_unknown_boundary() -> None:
    inline = read_jsonc_object(Path("configs/workspace_inline.jsonc"))
    schema = read_jsonc_object(Path("configs/workspace_schema.jsonc"))
    merged = merge_json_objects(
        inline,
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
    jsonschema.validate(merged, schema)
    invalid = merge_json_objects(
        inline,
        {
            "context": {
                "resource_activation": {
                    "default_boundary": "request_count",
                }
            }
        },
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)
