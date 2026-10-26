"""真实 SDK、stdio owner、固定信封与 Server 收据，不使用模型或替换执行链。"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    sealed_extension_catalog_binding_resolver,
)
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError
from app.services.infrastructure.mcp.extension_catalog import extension_schema_hash
from tests.harness.python.run_context import TestRunContext


@dataclass
class SchemaServerCase:
    owner: McpCatalogOwner
    definitions: Path
    receipts: Path

    @asynccontextmanager
    async def started(self) -> AsyncIterator[SchemaServerCase]:
        # MCP/AnyIO session 的进入、使用和关闭必须属于同一测试 task。
        await self.owner.start()
        try:
            yield self
        finally:
            await self.owner.shutdown()

    def publish(self, schema: dict[str, object]) -> None:
        self.definitions.write_text(json.dumps({"probe": schema}), encoding="utf-8")

    def received(self) -> list[dict[str, object]]:
        if not self.receipts.exists():
            return []
        return [json.loads(line) for line in self.receipts.read_text(encoding="utf-8").splitlines()]

    def invoker(self):
        return create_extension_tool_invoker_tool(
            self.owner.get_tools(),
            catalog_binding_resolver=sealed_extension_catalog_binding_resolver(self.owner.binding_snapshot()),
        )


@pytest.fixture
def schema_server_case(request: pytest.FixtureRequest) -> SchemaServerCase:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    definitions = context.artifacts_dir / "definitions.json"
    receipts = context.artifacts_dir / "receipts.jsonl"
    definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
    owner = McpCatalogOwner(
        raw_config={"servers": {"schema": {
            "enabled": True, "transport": "stdio", "command": sys.executable,
            "args": [str(context.project_root / "tests/integration/workspace_services/mcp/schema_server.py"),
                     str(definitions), str(receipts)],
            "cwd": str(context.project_root),
        }}},
        workspace_root=context.workspace_root, event_service=EventChannelService(),
    )
    return SchemaServerCase(owner, definitions, receipts)


@pytest.mark.parametrize("schema, valid, invalid", [
    ({"type": "object", "required": ["value"]}, {"value": "yes", "extra": 1}, {}),
    ({"type": "object", "additionalProperties": {"type": "string"}},
     {"runtime": "public", "callbacks": "public", "extra": "保留"}, {"extra": 2}),
    ({"type": "object", "$defs": {"kind": {"enum": ["a", "b"]}},
      "properties": {"nested": {"type": "object", "properties": {"kind": {"$ref": "#/$defs/kind"}},
                                  "required": ["kind"], "additionalProperties": False}},
      "required": ["nested"], "additionalProperties": False},
     {"nested": {"kind": "a"}}, {"nested": {"kind": "c"}}),
    ({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
      "oneOf": [{"required": ["a"]}, {"required": ["b"]}], "additionalProperties": False},
     {"a": "yes"}, {"a": "yes", "b": 1}),
    ({"type": "object", "properties": {"value": {"type": "string"}},
      "required": ["value"], "additionalProperties": False}, {"value": "yes"}, {"value": "yes", "extra": 1}),
])
async def test_full_contract_reaches_handler_and_invalid_call_is_paired(
    schema_server_case: SchemaServerCase, schema, valid, invalid,
) -> None:
    async with schema_server_case.started() as case:
        case.publish(schema)
        await case.owner.relist_servers_without_notifications()
        target = case.owner.get_tools()[0]
        assert case.owner.binding_snapshot().resolve(target.name).schema_hash == extension_schema_hash(
            export_model_tool_json_schema(target),
        )
        invoker = case.invoker()
        accepted = await invoker.ainvoke({
            "type": "tool_call", "id": "schema-valid", "name": invoker.name,
            "args": {"tool_name": target.name, "arguments": valid},
        })
        assert isinstance(accepted, ToolMessage)
        assert accepted.tool_call_id == "schema-valid"
        assert accepted.status == "success"
        assert case.received() == [{"name": "probe", "arguments": valid}]
        rejected = await invoker.ainvoke({
            "type": "tool_call", "id": "schema-invalid", "name": invoker.name,
            "args": {"tool_name": target.name, "arguments": invalid},
        })
        assert isinstance(rejected, ToolMessage)
        assert rejected.tool_call_id == "schema-invalid"
        assert rejected.status == "error"
        assert "参数校验失败" in rejected.content
        assert case.received() == [{"name": "probe", "arguments": valid}]


@pytest.mark.parametrize("changes", [
    {"required": ["value"]},
    {"additionalProperties": False},
    {"$defs": {"value": {"enum": ["a"]}}},
    {"anyOf": [{"required": ["value"]}]},
])
async def test_top_level_contract_change_moves_identity_and_rejects_old_binding(
    schema_server_case: SchemaServerCase, changes,
) -> None:
    async with schema_server_case.started() as case:
        schema = {"type": "object", "properties": {"value": {"type": "string"}}}
        case.publish(schema)
        await case.owner.relist_servers_without_notifications()
        sealed = case.owner.binding_snapshot()
        old_hash = sealed.resolve("mcp__schema__probe").schema_hash
        case.publish({**schema, **changes})
        await case.owner.relist_servers_without_notifications()
        current = case.owner.binding_snapshot()
        assert current.catalog_revision != sealed.catalog_revision
        assert current.resolve("mcp__schema__probe").schema_hash != old_hash
        invoker = create_extension_tool_invoker_tool(
            case.owner.get_tools(), catalog_binding_resolver=sealed_extension_catalog_binding_resolver(sealed),
        )
        result = await invoker.ainvoke({
            "type": "tool_call", "id": "stale-contract", "name": invoker.name,
            "args": {"tool_name": "mcp__schema__probe", "arguments": {"value": "a"}},
        })
        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == "stale-contract" and result.status == "error"
        assert "schema 不一致" in result.content
        assert case.received() == []


@pytest.mark.parametrize("schema", [
    {"type": "object", "$schema": "https://unsupported.invalid/schema"},
    {"type": "object", "$ref": "https://offline.invalid/schema"},
    {"type": "object", "$ref": "#/$defs/missing"},
])
async def test_invalid_schema_refresh_fails_without_partial_publication(
    schema_server_case: SchemaServerCase, schema,
) -> None:
    async with schema_server_case.started() as case:
        revision = case.owner.catalog_revision()
        case.publish(schema)
        with pytest.raises(McpCatalogRelistError):
            await case.owner.relist_servers_without_notifications()
        assert case.owner.catalog_revision() == revision
        assert case.received() == []
