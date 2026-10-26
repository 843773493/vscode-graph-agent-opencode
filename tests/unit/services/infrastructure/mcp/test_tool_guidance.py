"""统一扩展导航有界，完整定义只按有效可见性投递。"""

from __future__ import annotations

import json

import pytest

from app.agents.policy.tool_capability import ToolMetadata, ToolPolicyResolver
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.mcp.extension_catalog import extension_schema_hash
from app.services.infrastructure.mcp.tool_definitions import ToolDefinition
from app.services.infrastructure.mcp.tool_guidance import (
    MAX_NAVIGATION_DESCRIPTION_LENGTH,
    MAX_NAVIGATION_GROUPS,
    extension_tool_guidance,
)


@pytest.fixture
def definition_factory():
    def create(name, *, group="mcp:mini", description="读取 CI 日志", schema=None, origin="mcp"):
        schema = schema or {
            "type": "object", "$defs": {"mode": {"enum": ["read", "write"]}},
            "properties": {"mode": {"$ref": "#/$defs/mode"}}, "required": ["mode"],
            "additionalProperties": {"type": "string"},
        }
        return ToolDefinition(
            ToolMetadata(name, origin, "extension", group), extension_schema_hash(schema),
            canonical_json_bytes({"description": description, "input_schema": schema,
                                  "kind": "extension", "group_id": group}),
        )
    return create


@pytest.fixture
def policies_for():
    def resolve(definitions, *, visible=(), hidden=(), disabled=()):
        resolver = ToolPolicyResolver(
            policy_defaults={"model_visible": False}, policy_rules={},
            restrictions={"model_hidden": list(hidden), "execution_disabled": list(disabled)},
        )
        return {entry.metadata.tool_id: resolver.resolve(
            entry.metadata, model_visibility_override=entry.metadata.tool_id in visible,
        ) for entry in definitions}
    return resolve


def test_navigation_is_bounded_and_deterministic_without_target_schemas(definition_factory, policies_for):
    definitions = tuple(definition_factory(f"tool_{group}_{index}", group=f"group:{group:03d}")
                        for group in range(40) for index in range(30))
    policies = policies_for(definitions)
    content = extension_tool_guidance(definitions, policies)
    assert extension_tool_guidance(tuple(reversed(definitions)), policies) == content
    body = json.loads(content)
    assert len(body["navigation"]) == MAX_NAVIGATION_GROUPS
    assert body["additional_groups"] == 40 - MAX_NAVIGATION_GROUPS
    assert all(group["tool_count"] == 30 for group in body["navigation"])
    assert body["automatic_definitions"] == []
    assert not any(definition.metadata.tool_id in content for definition in definitions)
    assert "input_schema" not in content
    assert len(content.encode()) < 4096


def test_explicit_visibility_includes_complete_contract_and_local_extensions(definition_factory, policies_for):
    definitions = (definition_factory("hidden"), definition_factory("visible", origin="custom"))
    body = json.loads(extension_tool_guidance(definitions, policies_for(definitions, visible=("visible",))))
    automatic, = body["automatic_definitions"]
    assert automatic["tool_name"] == "visible"
    assert automatic["input_schema"] == definitions[1].payload["input_schema"]
    assert automatic["schema_hash"] == definitions[1].schema_hash
    assert body["navigation"][0]["tool_count"] == 2
    assert "skill_load" in body["discovery"]


def test_hard_hidden_and_disabled_do_not_leak_through_navigation(definition_factory, policies_for):
    definitions = (definition_factory("secret", group="secret-group", description="秘密能力"),
                   definition_factory("disabled", group="disabled-group", description="禁用能力"),
                   definition_factory("soft", group="visible-group", description="可搜索能力"))
    content = extension_tool_guidance(definitions, policies_for(
        definitions, visible=("secret", "disabled"), hidden=("secret",), disabled=("disabled",),
    ))
    assert "秘密" not in content and "secret" not in content
    assert "disabled" not in content and "禁用" not in content
    assert json.loads(content)["navigation"] == [
        {"group": "visible-group", "tool_count": 1, "example_capability": "可搜索能力"},
    ]


def test_untrusted_navigation_text_is_bounded_and_sanitized(definition_factory, policies_for):
    definitions = (definition_factory("echo", description="a\x00b\x1fc" + "长" * 500),)
    body = json.loads(extension_tool_guidance(definitions, policies_for(definitions)))
    description = body["navigation"][0]["example_capability"]
    assert len(description) == MAX_NAVIGATION_DESCRIPTION_LENGTH
    assert not any(ord(char) < 0x20 or ord(char) == 0x7F for char in description)


def test_empty_catalog_has_stable_discovery_instructions():
    first = extension_tool_guidance((), {})
    body = json.loads(first)
    assert first == extension_tool_guidance((), {})
    assert body["navigation"] == [] and body["automatic_definitions"] == []
    assert body["additional_groups"] == 0


def test_incomplete_policy_input_fails_explicitly(definition_factory):
    with pytest.raises(ValueError, match="完整有效策略"):
        extension_tool_guidance((definition_factory("missing"),), {})
