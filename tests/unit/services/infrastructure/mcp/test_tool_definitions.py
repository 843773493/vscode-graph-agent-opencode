"""工具与 Skill 关联的精确来源和 schema 身份判定。"""

from dataclasses import replace

from app.services.infrastructure.mcp.tool_definitions import (
    PluginToolSource,
    SkillToolSource,
    ToolDefinition,
    _tool_skill_associations,
)


def test_plugin_tool_association_requires_exact_source_and_schema_identity():
    from app.agents.policy.tool_capability import ToolMetadata
    from app.domain.itemized.hashing import canonical_json_bytes
    from app.schemas.gateway.service_plugin import PluginToolBinding
    from app.schemas.gateway.skill_sources import PluginSkillSourceDTO
    from app.services.infrastructure.mcp.extension_catalog import extension_schema_hash

    tool_id = "mcp__browser__navigate"
    schema = {"type": "object", "properties": {"url": {"type": "string"}}}
    schema_hash = extension_schema_hash(schema)
    plugin = PluginSkillSourceDTO(
        package_id="browser-tools", plugin_id="browser", package_version="1.2.0",
        package_digest="a" * 64, contribution_id="browser.skills", workspace_id=None,
        tool_bindings=[PluginToolBinding(
            tool_id=tool_id, service_id="browser-mcp", contract_version=1, schema_hash=schema_hash,
        )],
    )
    source = SkillToolSource(
        name="Browser Guide", source_id="gateway-skills:v1", entry_identity="browser-guide:v1",
        display_uri="boxteam://gateway/test/resources/skills/Browser%20Guide/SKILL.md", plugin=plugin,
    )
    definition = ToolDefinition(
        metadata=ToolMetadata(tool_id, "mcp", "tool", "browser"), schema_hash=schema_hash,
        description="navigate", _input_schema_json=canonical_json_bytes(schema),
    )

    missing_source, = _tool_skill_associations(tool_id, definition, (source,), (), gateway_id="gateway-1")
    assert (missing_source.status, missing_source.reason) == ("unavailable", "plugin-service-source-unverified")

    exact_source = PluginToolSource(
        gateway_id="gateway-1", package_id="browser-tools", plugin_id="browser", package_version="1.2.0",
        package_digest="a" * 64, service_id="browser-mcp", workspace_id=None, contract_version=1,
    )
    verified_definition = ToolDefinition(
        metadata=definition.metadata, schema_hash=schema_hash, description=definition.description,
        _input_schema_json=definition._input_schema_json, plugin_tool_source=exact_source,
    )
    available, = _tool_skill_associations(tool_id, verified_definition, (source,), (), gateway_id="gateway-1")
    assert (available.status, available.reason) == ("available", None)
    assert available.tool_source == exact_source
    deduplicated, = _tool_skill_associations(
        tool_id, verified_definition, (source,), ("Browser Guide",), gateway_id="gateway-1",
    )
    assert deduplicated.status == "available"

    for invalid_source in (
        PluginToolSource(**{**exact_source.to_dict(), "gateway_id": "other-gateway"}),
        PluginToolSource(**{**exact_source.to_dict(), "package_digest": "b" * 64}),
        PluginToolSource(**{**exact_source.to_dict(), "service_id": "other-service"}),
        PluginToolSource(**{**exact_source.to_dict(), "contract_version": 2}),
    ):
        invalid_definition = ToolDefinition(
            metadata=definition.metadata, schema_hash=schema_hash, description=definition.description,
            _input_schema_json=definition._input_schema_json, plugin_tool_source=invalid_source,
        )
        unavailable, = _tool_skill_associations(
            tool_id, invalid_definition, (source,), (), gateway_id="gateway-1",
        )
        assert (unavailable.status, unavailable.reason) in {
            ("unavailable", "plugin-service-source-mismatch"),
            ("unavailable", "tool-contract-version-unsupported"),
        }

    shadowed, = _tool_skill_associations(
        tool_id, verified_definition, (replace(source, effective=False),), (), gateway_id="gateway-1",
    )
    assert (shadowed.status, shadowed.reason) == ("unavailable", "skill-source-not-effective")

    wrong_schema_definition = ToolDefinition(
        metadata=definition.metadata, schema_hash="sha256:" + "b" * 64, description=definition.description,
        _input_schema_json=definition._input_schema_json, plugin_tool_source=exact_source,
    )
    mismatched, = _tool_skill_associations(tool_id, wrong_schema_definition, (source,), (), gateway_id="gateway-1")
    assert (mismatched.status, mismatched.reason) == ("unavailable", "tool-schema-hash-mismatch")
