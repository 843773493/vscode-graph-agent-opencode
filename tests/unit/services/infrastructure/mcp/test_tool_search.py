"""通过真实发布、freeze/seal 和 Saver 验证发现；不构造替代目录。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy.tool_capability import ToolPolicyResolver
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.tools.tool_discovery import SearchToolsInput, create_search_tools_tool
from app.core.lifecycle import LifetimeScope
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogResourcePublisher
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
)
from app.services.infrastructure.mcp.tool_definitions import (
    definition_resource_id,
    tool_catalog_from_activation,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.tool_output_store import ToolOutputStore
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 当前测试路径的正式 SQLite fixture
)
from tests.support.mcp.session_doubles import drain_pending_relists
from tests.unit.services.infrastructure.mcp.test_catalog_activation import (
    CatalogSessionFactory,
)
from tests.unit.services.infrastructure.mcp.test_catalog_activation import (
    catalog_workspace as catalog_workspace,  # noqa: PLC0414 - 完整工作区 fixture
)
from tests.unit.services.infrastructure.mcp.test_catalog_activation import (
    guidance_prepare as guidance_prepare,  # noqa: PLC0414 - 既有真实 freeze/seal 链路
)


@pytest.fixture
async def catalog_runtime(catalog_workspace, extension_catalog_policy_resolver, request, monkeypatch):
    factory = CatalogSessionFactory()
    factory.session.description = "查找 CI 失败记录与日志"
    owner = McpCatalogOwner(
        leases=ExternalResourceLeaseLedger(),
        raw_config={"servers": {"mini": {"enabled": True, "transport": "stdio", "command": "controlled-server"}}},
        workspace_root=catalog_workspace, event_service=EventChannelService(), session_factory=factory,
    )
    calls = []

    def execute(**arguments):
        calls.append(arguments)
        return arguments

    schema = {
        "type": "object", "$defs": {"run": {"enum": ["failed", "passed"]}},
        "properties": {"run": {"$ref": "#/$defs/run"}}, "required": ["run"],
        "additionalProperties": {"type": "string"},
    }
    custom = tuple(StructuredTool.from_function(
        func=execute, name=name, description=description,
        args_schema={**schema, **({"$defs": {"run": {"enum": ["failed", "passed"], "description": "完整大定义" * 6000}}} if name == "huge" else {})},
    ) for name, description in (
        ("ci_logs", "读取 CI 失败日志"), ("ci_runs", "查找 CI 失败执行"),
        ("notes", "读取笔记"), ("huge", "读取大定义"),
        ("hidden_ci", "CI 失败日志硬隐藏"),
    ))
    await owner.start()
    registry = ResourceRegistry(publication_lock=owner.publication_lock, capture_validator=owner.validate_resource_capture)
    scope = LifetimeScope("tool-discovery")
    # 历史读取测试从 publisher 首次登记起生成 v2 资产，避免改写已登记的 descriptor。
    with monkeypatch.context() as historical_writer:
        if getattr(request, "param", None) == "v2":
            from app.services.infrastructure.mcp import catalog_activation
            historical_writer.setattr(catalog_activation, "EXTENSION_CATALOG_SOURCE_ID", "extension-catalog:v2")
        publisher = McpCatalogResourcePublisher(registry=registry, custom_tools=custom, policy_resolver=extension_catalog_policy_resolver)
        scope.register(owner.subscribe_publication(publisher.publish), label="discovery-publication")
    try:
        yield owner, factory, registry, scope
        assert calls == [], "发现不得执行候选"
    finally:
        await scope.close()
        await owner.shutdown()


@pytest.fixture
def discovery_store(catalog_workspace):
    return ToolOutputStore(workspace_root=catalog_workspace)


@pytest.fixture
def discovery_tool_factory(discovery_store, registry_db):
    _saver, session, _connection, _accepted = registry_db

    async def materialize(definition, entry):
        return discovery_store.materialize(
            session_id=session, tool_name="search_tools", tool_call_id="discovery-fixture",
            text=canonical_json_bytes(entry).decode(),
            output_key=f"discovery-fixture:{definition.provenance.resource_id}:{definition.provenance.revision}",
        )

    def build(*, catalog_resolver, policy_resolver, definition_writer=materialize, response_byte_limit=None):
        return create_search_tools_tool(
            catalog_resolver=catalog_resolver, policy_resolver=policy_resolver,
            definition_writer=definition_writer,
            response_byte_limit=discovery_store.inline_byte_limit if response_byte_limit is None else response_byte_limit,
        )

    return build


@pytest.fixture
def discovery_case(guidance_prepare, registry_db, discovery_tool_factory):
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    _wire, frozen = guidance_prepare("discovery:initial", boundary="turn")

    def read(snapshot, binding):
        return saver.read_resource_activation_payload(snapshot, binding, storage_owner=(session, thread))

    catalog = tool_catalog_from_activation(frozen, read)
    policy_resolver = ToolPolicyResolver(
        policy_defaults={"model_visible": False}, policy_rules={},
        restrictions={"execution_disabled": ["notes"], "model_hidden": ["hidden_ci"]},
    )
    policies = {definition.metadata.tool_id: policy_resolver.resolve(definition.metadata) for definition in catalog.definitions}
    tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda _: policies)
    return tool, catalog, policies, frozen, read


@pytest.mark.parametrize("arguments", [
    {}, {"query": ""}, {"query": "   "}, {"tool_ids": []}, {"tool_ids": [""]},
    {"query": "CI", "tool_ids": ["ci_logs"]}, {"query": "CI", "limit": True},
    {"query": "CI", "limit": 0}, {"query": "CI", "max_response_bytes": 2047},
    {"query": "CI", "unknown": True},
])
def test_invalid_selectors_and_budgets_are_rejected(arguments):
    with pytest.raises(ValidationError):
        SearchToolsInput.model_validate(arguments)


async def test_task_search_is_deterministic_cross_origin_and_keeps_hidden_schema(discovery_case):
    tool, catalog, policies, _frozen, _read = discovery_case
    args = {"query": "CI 失败", "limit": 20}
    first = await tool.ainvoke(args)
    assert first == await tool.ainvoke(args)
    result = json.loads(first)
    assert {entry["origin"] for entry in result["results"]} == {"custom", "mcp"}
    assert {entry["tool_id"] for entry in result["results"]} == {"ci_logs", "ci_runs", "mcp__mini__echo"}
    assert all(entry["effective_policy"]["execution_enabled"] and not entry["effective_policy"]["model_visible"]
               and entry["match_reasons"] for entry in result["results"])
    assert result["catalog_revision"] == catalog.catalog_revision
    assert policies["notes"].execution_enabled is False
    assert policies["hidden_ci"].execution_enabled is True
    definition = next(entry for entry in result["results"] if entry["tool_id"] == "ci_logs")
    assert definition["input_schema"]["$defs"] == {"run": {"enum": ["failed", "passed"]}}
    assert definition["input_schema"]["required"] == ["run"]
    assert definition["input_schema"]["additionalProperties"] == {"type": "string"}
    assert json.loads(await tool.ainvoke({"query": "不存在的任务"}))["results"] == []


async def test_exact_ids_keep_order_errors_and_do_not_disclose_disabled_schema(discovery_case):
    tool, _catalog, _policies, _frozen, _read = discovery_case
    ids = ["notes", "missing", "ci_logs", "ci_logs", "mcp__mini__echo", "hidden_ci"]
    result = json.loads(await tool.ainvoke({"tool_ids": ids, "limit": 20}))
    assert [entry["tool_id"] for entry in result["results"]] == ids
    assert result["results"][0]["error"]["code"] == "tool-disabled"
    assert result["results"][1]["error"]["code"] == "tool-not-found"
    assert all("input_schema" not in entry for entry in result["results"][:2])
    assert result["results"][2] == result["results"][3]
    assert result["results"][5]["error"]["code"] == "tool-model-hidden"
    assert "input_schema" not in result["results"][5]


async def test_pagination_reduces_entries_without_truncating_schema(discovery_case, discovery_store, registry_db):
    tool, _catalog, _policies, _frozen, _read = discovery_case
    args = {"tool_ids": ["ci_logs", "ci_runs", "mcp__mini__echo"], "limit": 20, "max_response_bytes": 2048}
    pages = []
    cursor = None
    while True:
        raw = await tool.ainvoke({**args, **({"cursor": cursor} if cursor else {})})
        assert len(raw.encode()) <= 2048
        page = json.loads(raw)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(pages) > 1
    assert [entry["tool_id"] for page in pages for entry in page["results"]] == args["tool_ids"]
    assert all(entry["status"] == "available" for page in pages for entry in page["results"])
    result = json.loads(await tool.ainvoke({"tool_ids": ["huge", "ci_logs"], "max_response_bytes": 4096}))
    oversized = result["results"][0]
    assert oversized["schema_state"] == "needs_read"
    assert oversized["byte_count"] > 4096
    assert "input_schema" not in oversized
    _saver, session, _connection, _accepted = registry_db
    stored = discovery_store.read(session, oversized["read_path"], oversized["byte_count"]).data
    assert hashlib.sha256(stored).hexdigest() == oversized["content_sha256"]
    assert json.loads(stored)["input_schema"]["$defs"]["run"]["description"] == "完整大定义" * 6000


async def test_actual_output_middleware_keeps_schema_reference_complete(discovery_case, discovery_store, registry_db):
    tool, _catalog, _policies, _frozen, _read = discovery_case
    _saver, session, _connection, _accepted = registry_db
    args = {"tool_ids": ["huge"], "max_response_bytes": 262144}
    call = {"type": "tool_call", "id": "large-discovery", "name": tool.name, "args": args}
    message = await tool.ainvoke(call)
    middleware = ToolOutputMiddleware(session_id=session, store=discovery_store)
    bounded = await middleware.awrap_tool_call(SimpleNamespace(tool_call=call), lambda _request: tool.ainvoke(call))
    assert bounded == message
    assert len(bounded.content.encode()) <= discovery_store.inline_byte_limit
    entry, = json.loads(bounded.content)["results"]
    assert entry["schema_state"] == "needs_read" and "input_schema" not in entry
    original = discovery_store.read(session, entry["read_path"], entry["byte_count"]).data
    assert hashlib.sha256(original).hexdigest() == entry["content_sha256"]


async def test_materialization_failure_is_paired_and_never_forges_reference(discovery_case, discovery_tool_factory):
    _tool, catalog, policies, _frozen, _read = discovery_case

    async def failed(_definition, _entry):
        raise PermissionError("controlled write failure")

    tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda _: policies, definition_writer=failed)
    message = await tool.ainvoke({"type": "tool_call", "id": "write-failure", "name": tool.name,
                                 "args": {"tool_ids": ["huge"], "max_response_bytes": 4096}})
    assert message.tool_call_id == "write-failure" and message.status == "error"
    assert "definition-materialization-failed" in message.content and "schema_ref" not in message.content


async def test_reference_exceeding_effective_policy_budget_is_an_explicit_error(discovery_case, discovery_tool_factory):
    _tool, catalog, policies, _frozen, _read = discovery_case
    tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda _: policies, response_byte_limit=1024)
    raw = await tool.ainvoke({"tool_ids": ["huge"], "max_response_bytes": 4096})
    assert len(raw.encode()) <= 1024
    entry, = json.loads(raw)["results"]
    assert entry["status"] == "error" and entry["error"]["code"] == "definition-too-large"
    assert entry["error"]["required_response_bytes"] > 1024
    assert "input_schema" not in entry and "schema_ref" not in entry


async def test_model_call_boundary_replaces_full_schema_without_changing_provider_tools(
    catalog_runtime, guidance_prepare, registry_db,
):
    owner, factory, _registry, _scope = catalog_runtime
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)

    def catalog(snapshot):
        return tool_catalog_from_activation(
            snapshot, lambda frozen, binding: saver.read_resource_activation_payload(frozen, binding, storage_owner=(session, thread)),
        )

    before_wire, before = guidance_prepare("discovery:model-first", boundary="model_call")
    first = catalog(before)
    factory.session.minimum = 8
    factory.changed()
    await drain_pending_relists(owner)
    after_wire, after = guidance_prepare("discovery:model-second", boundary="model_call")
    updated = catalog(after)
    old_definition, = (entry for entry in first.definitions if entry.metadata.origin == "mcp")
    new_definition, = (entry for entry in updated.definitions if entry.metadata.origin == "mcp")
    assert old_definition.payload["input_schema"]["properties"]["text"]["minLength"] == 1
    assert new_definition.payload["input_schema"]["properties"]["text"]["minLength"] == 8
    assert old_definition.schema_hash != new_definition.schema_hash
    assert catalog(before) == first
    assert before_wire["tools"] == after_wire["tools"]
    original = saver.get_context_assembly(session, thread, assembly_id=before_wire["assembly_id"])
    current = saver.get_context_assembly(session, thread, assembly_id=after_wire["assembly_id"])
    assert original.seal_binding.prefix.state.epoch == current.seal_binding.prefix.state.epoch
    assert original.request_hash == current.request_hash


async def test_cursor_fences_query_policy_and_catalog_changes(discovery_case, discovery_tool_factory):
    tool, catalog, policies, _frozen, _read = discovery_case
    args = {"query": "CI", "limit": 1}
    cursor = json.loads(await tool.ainvoke(args))["next_cursor"]
    assert cursor
    for invalid in ("!", "", "a" * 1000):
        if invalid:
            result = await tool.ainvoke({**args, "cursor": invalid})
            assert "cursor" in result
    assert "查询" in await tool.ainvoke({**args, "query": "CI failed", "cursor": cursor})
    policies["ci_logs"] = replace(policies["ci_logs"], execution_enabled=False)
    assert "策略" in await tool.ainvoke({**args, "cursor": cursor})
    current = discovery_tool_factory(
        catalog_resolver=lambda: replace(catalog, catalog_revision="changed"), policy_resolver=lambda _: policies,
    )
    assert "目录" in await current.ainvoke({**args, "cursor": cursor})
    denied = json.loads(await tool.ainvoke({"tool_ids": ["ci_logs"]}))
    assert denied["results"][0]["error"]["code"] == "tool-disabled"


async def test_full_definition_freeze_survives_owner_update_delete_and_cold_saver(
    discovery_case, catalog_runtime, guidance_prepare, registry_db, discovery_tool_factory,
):
    tool, original, _policies, frozen, read = discovery_case
    _owner, factory, registry, _scope = catalog_runtime
    before = await tool.ainvoke({"tool_ids": ["mcp__mini__echo"]})
    factory.session.minimum = 10
    factory.session.empty = True
    factory.changed()
    await drain_pending_relists(_owner)
    assert definition_resource_id("mcp__mini__echo") not in {item.resource_id for item in registry.capture().snapshots}
    _wire, same_turn = guidance_prepare("discovery:same-turn", boundary="turn")
    assert same_turn == frozen
    assert tool_catalog_from_activation(same_turn, read) == original
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        restored = tool_catalog_from_activation(
            frozen, lambda snapshot, binding: cold.read_resource_activation_payload(
                snapshot, binding, storage_owner=(session, thread),
            ),
        )
    cold_tool = discovery_tool_factory(catalog_resolver=lambda: restored, policy_resolver=lambda _: _policies)
    assert await cold_tool.ainvoke({"tool_ids": ["mcp__mini__echo"]}) == before
    assert restored == original
    without_definition = replace(frozen, bindings=tuple(
        binding for binding in frozen.bindings if binding.resource_id != definition_resource_id("ci_logs")
    ))
    with pytest.raises(ExtensionCatalogUnavailableError, match="完整定义"):
        tool_catalog_from_activation(without_definition, read)


async def test_snapshot_failure_remains_paired_tool_error(discovery_case, discovery_tool_factory):
    tool, catalog, policies, _frozen, _read = discovery_case

    def unavailable():
        raise ExtensionCatalogUnavailableError("冻结完整定义不可恢复")

    broken = discovery_tool_factory(catalog_resolver=unavailable, policy_resolver=lambda _: policies)
    result = await broken.ainvoke({"type": "tool_call", "id": "discovery-error", "name": tool.name, "args": {"query": "CI"}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert result.tool_call_id == "discovery-error"
    assert "不可恢复" in result.content
    assert "ci_logs" not in json.dumps(export_model_tool_json_schema(tool))
    assert catalog.definitions
