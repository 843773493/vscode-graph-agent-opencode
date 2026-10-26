"""真实 SDK、stdio owner、固定信封与 Server 收据，不使用模型或替换执行链。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy.tool_capability import ToolPolicyResolver
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    sealed_extension_catalog_binding_resolver,
)
from app.agents.tools.tool_discovery import create_search_tools_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.lifecycle import LifetimeScope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
    McpCatalogResourcePublisher,
    extension_dispatch_from_activation,
)
from app.services.infrastructure.mcp.extension_catalog import extension_schema_hash
from app.services.infrastructure.mcp.tool_definitions import (
    tool_catalog_from_activation,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.tool_output_store import ToolOutputStore
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 当前正式测试路径的 Saver fixture
)


@dataclass
class SchemaServerCase:
    owner: McpCatalogOwner
    definitions: Path
    receipts: Path
    workspace_root: Path

    @asynccontextmanager
    async def started(self) -> AsyncIterator[SchemaServerCase]:
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
            target_resolver=self.owner.resolve_execution_target,
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
    return SchemaServerCase(owner, definitions, receipts, context.workspace_root)


async def test_real_stdio_lifecycle_is_owned_across_distinct_caller_tasks(
    schema_server_case: SchemaServerCase,
) -> None:
    case = schema_server_case
    await asyncio.create_task(case.owner.start())
    runtimes = tuple(case.owner._runtimes.values())
    try:
        case.publish({"type": "object", "required": ["text"]})
        await asyncio.create_task(case.owner.relist_servers_without_notifications())
        result = await asyncio.create_task(case.invoker().ainvoke({
            "tool_name": "mcp__schema__probe", "arguments": {"text": "独立调用 task"},
        }))
        assert result
        assert case.received() == [{"name": "probe", "arguments": {"text": "独立调用 task"}}]
    finally:
        await asyncio.create_task(case.owner.shutdown())
    assert not case.owner.started
    for runtime in runtimes:
        assert runtime._task is not None and runtime._task.done() and not runtime.running


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
            target_resolver=case.owner.resolve_execution_target,
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


@pytest.mark.parametrize("large_schema", [False, True])
async def test_search_frozen_full_definition_then_execute_real_stdio(
    schema_server_case: SchemaServerCase, registry_db, frozen_provider_activation, large_schema: bool,
    extension_catalog_policy_resolver,
) -> None:
    async with schema_server_case.started() as case:
        schema = {
            "type": "object", "$defs": {"mode": {"enum": ["read", "write"]}},
            "properties": {"mode": {"$ref": "#/$defs/mode"}}, "required": ["mode"],
            "additionalProperties": {"type": "string"},
        }
        if large_schema:
            schema["$defs"]["mode"]["description"] = "完整大定义" * 6000
        case.publish(schema)
        await case.owner.relist_servers_without_notifications()
        registry = ResourceRegistry(publication_lock=case.owner.publication_lock, capture_validator=case.owner.validate_resource_capture)
        publisher = McpCatalogResourcePublisher(registry=registry, policy_resolver=extension_catalog_policy_resolver)
        scope = LifetimeScope("stdio-discovery")
        scope.register(case.owner.subscribe_publication(publisher.publish), label="stdio-publication")
        try:
            saver, session, _connection, accepted = registry_db
            thread = saver._storage.storage.main_thread_id(session)
            admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])
            with (session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
                  execution_admission_scope(admission, langgraph_thread_id=thread)):
                activation = frozen_provider_activation(saver, registry=registry, plan_creation_idempotency_key="stdio-discovery")
            frozen = activation["activation_snapshot"]

            def catalog(owner):
                return tool_catalog_from_activation(
                    frozen, lambda snapshot, binding: owner.read_resource_activation_payload(snapshot, binding, storage_owner=(session, thread)),
                )

            resolver = ToolPolicyResolver(policy_defaults={"model_visible": False}, policy_rules={}, restrictions={})
            store = ToolOutputStore(workspace_root=case.workspace_root)

            async def materialize(definition, entry):
                return store.materialize(
                    session_id=session, tool_name="search_tools", tool_call_id="search-stdio",
                    text=canonical_json_bytes(entry).decode(),
                    output_key=f"search-stdio:{definition.provenance.resource_id}:{definition.provenance.revision}",
                )

            search = create_search_tools_tool(
                catalog_resolver=lambda: catalog(saver),
                policy_resolver=lambda frozen_catalog: {
                    definition.metadata.tool_id: resolver.resolve(definition.metadata) for definition in frozen_catalog.definitions
                },
                definition_writer=materialize, response_byte_limit=store.inline_byte_limit,
            )
            found = await search.ainvoke({"type": "tool_call", "id": "search-stdio", "name": search.name,
                                         "args": {"query": "probe"}})
            assert isinstance(found, ToolMessage) and found.tool_call_id == "search-stdio"
            entry, = json.loads(found.content)["results"]
            target = case.owner.get_tools()[0]
            if large_schema:
                assert entry["schema_state"] == "needs_read" and "input_schema" not in entry
                full = json.loads(store.read(session, entry["read_path"], entry["byte_count"]).data)
            else:
                assert entry["schema_state"] == "inline_complete"
                full = entry
            assert full["input_schema"] == export_model_tool_json_schema(target)
            assert entry["schema_hash"] == case.owner.binding_snapshot().resolve(target.name).schema_hash
            assert entry["effective_policy"]["model_visible"] is False
            assert case.received() == []
            parent, = (binding for binding in frozen.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
            dispatch = extension_dispatch_from_activation(
                snapshot=frozen, binding=parent, model_call_id=activation["model_call_id"],
                payload=saver.read_resource_activation_payload(frozen, parent, storage_owner=(session, thread)),
            )
            invoker = create_extension_tool_invoker_tool(
                case.owner.get_tools(), catalog_binding_resolver=sealed_extension_catalog_binding_resolver(dispatch.binding_ref),
                target_resolver=case.owner.resolve_execution_target,
            )
            result = await invoker.ainvoke({
                "type": "tool_call", "id": "invoke-after-search", "name": entry["invocation"]["entry_point"],
                "args": {"tool_name": entry["tool_id"], "arguments": {"mode": "read", "runtime": "原样传递", "extra": "保留"}},
            })
            assert isinstance(result, ToolMessage) and result.status == "success"
            assert result.tool_call_id == "invoke-after-search"
            assert case.received() == [{"name": "probe", "arguments": {"mode": "read", "runtime": "原样传递", "extra": "保留"}}]
            with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
                assert catalog(cold) == catalog(saver)
        finally:
            await scope.close()


async def test_real_stdio_restart_rejects_sealed_old_handle_and_executes_fresh_binding(
    schema_server_case: SchemaServerCase,
) -> None:
    async with schema_server_case.started() as case:
        sealed = case.owner.binding_snapshot()
        invoker = case.invoker()
        await case.owner.shutdown()
        await case.owner.start()
        fresh = case.owner.binding_snapshot()
        assert fresh.catalog_revision == sealed.catalog_revision
        assert fresh.resolve("mcp__schema__probe").schema_hash == sealed.resolve("mcp__schema__probe").schema_hash
        rejected = await invoker.ainvoke({
            "type": "tool_call", "id": "old-connection", "name": invoker.name,
            "args": {"tool_name": "mcp__schema__probe", "arguments": {"text": "旧调用"}},
        })
        assert isinstance(rejected, ToolMessage)
        assert rejected.tool_call_id == "old-connection" and rejected.status == "error"
        assert "原连接" in rejected.content
        assert case.received() == [], "旧调用不能发送到新进程"
        accepted = await case.invoker().ainvoke({
            "type": "tool_call", "id": "new-connection", "name": invoker.name,
            "args": {"tool_name": "mcp__schema__probe", "arguments": {"text": "新调用"}},
        })
        assert isinstance(accepted, ToolMessage)
        assert accepted.tool_call_id == "new-connection" and accepted.status == "success"
        assert case.received() == [{"name": "probe", "arguments": {"text": "新调用"}}]
