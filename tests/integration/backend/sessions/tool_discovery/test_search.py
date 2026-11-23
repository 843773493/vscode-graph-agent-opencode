"""通过真实发布、freeze/seal 和 Saver 验证发现；不构造替代目录。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy.tool_capability import ToolPolicyResolver
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.services.business.agent_tool_settings.catalog import AgentToolCatalog
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp import McpCatalogResourcePublisher
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
    ExtensionTargetResolutionError,
)
from app.services.infrastructure.mcp.frozen_catalog import (
    restore_frozen_tool_catalog,
)
from app.services.infrastructure.mcp.tool_definitions import (
    SkillToolSource,
    definition_resource_id,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 复用真实原生日志写入故障。
)
from tests.support.mcp.session_doubles import drain_pending_relists


@pytest.fixture
def management_catalog(catalog_workspace, catalog_runtime):
    owner, _factory, _registry, _scope = catalog_runtime
    config_path = catalog_workspace / "workspace.jsonc"
    config_path.write_text("{}", encoding="utf-8")
    config = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    config.get_snapshot()
    dependencies = MagicMock()
    dependencies.get_mcp_catalog_owner.return_value = owner
    return AgentToolCatalog(
        config_service=config, dependency_provider=dependencies,
        background_task_registry=MagicMock(), background_mailbox=MagicMock(),
        job_event_bus=MagicMock(), workspace_root=catalog_workspace,
    )


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


async def test_custom_skill_association_is_published_frozen_and_returned_by_search(
    catalog_runtime, prepare_tool_context, discovery_tool_factory, extension_catalog_policy_resolver, registry_db,
):
    _owner, _factory, registry, _scope = catalog_runtime

    def execute(text: str) -> str:
        return text

    custom = StructuredTool.from_function(execute, name="local_build", description="执行本地构建")
    publisher = McpCatalogResourcePublisher(
        registry=registry,
        custom_tools=(custom,),
        policy_resolver=extension_catalog_policy_resolver,
        custom_skill_bindings={"local_build": ("Build Guide",), "missing_tool": ("Build Guide",)},
        skill_sources=(SkillToolSource(
            name="Build Guide", source_id="workspace-skills:v1",
            entry_identity="workspace-skill:build-guide:v1",
            display_uri="boxteam://workspace/test/resources/skills/Build%20Guide/SKILL.md",
        ),),
    )
    publisher.publish(_owner.publication_snapshot())
    _wire, frozen = prepare_tool_context("skill-tool-association:freeze", boundary="turn")
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    restored = restore_frozen_tool_catalog(
        frozen,
        lambda snapshot, binding: saver.read_resource_activation_payload(
            snapshot, binding, storage_owner=(session, thread),
        ),
    )
    definition, = (item for item in restored.definitions if item.metadata.tool_id == "local_build")
    assert [item.to_dict() for item in definition.skill_associations] == [{
        "skill_name": "Build Guide", "tool_id": "local_build", "source_id": "workspace-skills:v1",
        "entry_identity": "workspace-skill:build-guide:v1",
        "display_uri": "boxteam://workspace/test/resources/skills/Build%20Guide/SKILL.md",
        "status": "available",
    }]

    policies = {
        item.metadata.tool_id: extension_catalog_policy_resolver((item.metadata,))[item.metadata.tool_id]
        for item in restored.definitions
    }
    search = discovery_tool_factory(
        catalog_resolver=lambda: restored, policy_resolver=lambda _metadata: policies,
    )
    result = json.loads(await search.ainvoke({"tool_ids": ["local_build"]}))
    assert result["results"][0]["skill_names"] == ["Build Guide"]
    assert result["results"][0]["skill_associations"] == [definition.skill_associations[0].to_dict()]
    assert result["skill_associations"] == []
    missing = json.loads(await search.ainvoke({"tool_ids": ["missing_tool"]}))
    assert missing["skill_associations"] == [
        {
            "skill_name": "Build Guide", "tool_id": "missing_tool", "source_id": "workspace-skills:v1",
            "entry_identity": "workspace-skill:build-guide:v1",
            "display_uri": "boxteam://workspace/test/resources/skills/Build%20Guide/SKILL.md",
            "status": "unavailable", "reason": "tool-not-in-published-catalog",
        },
    ]


async def test_skill_associations_do_not_disclose_hidden_or_disabled_tool_names(
    catalog_runtime, prepare_tool_context, discovery_tool_factory, extension_catalog_policy_resolver, registry_db,
):
    owner, _factory, registry, _scope = catalog_runtime

    def execute() -> str:
        return "ok"

    tools = tuple(StructuredTool.from_function(execute, name=name, description=name)
        for name in ("hidden_skill_tool", "disabled_skill_tool", "visible_skill_tool"))
    publisher = McpCatalogResourcePublisher(
        registry=registry, custom_tools=tools, policy_resolver=extension_catalog_policy_resolver,
        custom_skill_bindings={item.name: ("Private Guide",) for item in tools},
        skill_sources=(SkillToolSource(name="Private Guide", source_id="workspace-skills:v1",
            entry_identity="private-guide:v1",
            display_uri="boxteam://workspace/test/resources/skills/Private%20Guide/SKILL.md"),),
    )
    publisher.publish(owner.publication_snapshot())
    _wire, frozen = prepare_tool_context("private-tool-association", boundary="turn")
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    catalog = restore_frozen_tool_catalog(frozen, lambda snapshot, binding: saver.read_resource_activation_payload(
        snapshot, binding, storage_owner=(session, thread)))
    resolver = ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={
        "execution_disabled": ["disabled_skill_tool"], "model_hidden": ["hidden_skill_tool"],
    })
    policies = {item.metadata.tool_id: resolver.resolve(item.metadata) for item in catalog.definitions}
    search = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda _metadata: policies)
    result = json.loads(await search.ainvoke({"query": "skill_tool"}))
    assert [item["tool_id"] for item in result["results"]] == ["visible_skill_tool"]
    assert result["skill_associations"] == []
    assert "hidden_skill_tool" not in json.dumps(result)
    assert "disabled_skill_tool" not in json.dumps(result)
    assert result["results"][0]["skill_names"] == ["Private Guide"]


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


@pytest.mark.parametrize("catalog_runtime", [{
    "execution_disabled": ["notes", "mcp__mini__echo"], "model_hidden": ["hidden_ci"],
}], indirect=True)
async def test_disabled_before_publication_remains_known_after_freeze_and_cold_read(
    catalog_runtime, prepare_tool_context, registry_db, discovery_tool_factory,
):
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    wire, frozen = prepare_tool_context("initially-disabled-catalog")
    assembly = saver.get_context_assembly(session, thread, assembly_id=wire["assembly_id"])
    policy = ToolPolicyResolver(policy_defaults={"model_visible": False}, policy_rules={}, restrictions={
        "execution_disabled": ["notes", "mcp__mini__echo"], "model_hidden": ["hidden_ci"],
    })
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        restored = cold.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        )
        assert restored == frozen
        catalog = restore_frozen_tool_catalog(restored, lambda snapshot, binding: cold.read_resource_activation_payload(
            snapshot, binding, storage_owner=(session, thread),
        ))
        tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda current: {
            entry.metadata.tool_id: policy.resolve(entry.metadata) for entry in current.definitions
        })
        exact = json.loads(await tool.ainvoke({"tool_ids": ["notes", "mcp__mini__echo", "hidden_ci", "unknown"]}))
        assert [entry["error"]["code"] for entry in exact["results"]] == [
            "tool-disabled", "tool-disabled", "tool-model-hidden", "tool-not-found",
        ]
        assert all("input_schema" not in entry and "description" not in entry for entry in exact["results"])
        ranked = json.loads(await tool.ainvoke({"query": "CI 失败", "limit": 20}))
        assert {entry["tool_id"] for entry in ranked["results"]} == {"ci_logs", "ci_runs"}


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
    call = ToolCall(type="tool_call", id="large-discovery", name=tool.name, args=args)
    message = await tool.ainvoke(call)
    middleware = ToolOutputMiddleware(session_id=session, store=discovery_store)
    request = ToolCallRequest(tool_call=call, tool=tool, state={}, runtime=Mock())
    bounded = await middleware.awrap_tool_call(request, lambda _request: tool.ainvoke(call))
    assert isinstance(bounded, ToolMessage) and isinstance(bounded.content, str)
    assert bounded == message
    assert len(bounded.content.encode()) <= discovery_store.inline_byte_limit
    entry, = json.loads(bounded.content)["results"]
    assert entry["schema_state"] == "needs_read" and "input_schema" not in entry
    original = discovery_store.read(session, entry["read_path"], entry["byte_count"]).data
    assert hashlib.sha256(original).hexdigest() == entry["content_sha256"]


@pytest.mark.parametrize("failure", [
    PermissionError(13, "拒绝写入", "/private/workspace/tool-results/definition.json"),
    OSError(22, "写入失败", r"C:\private\workspace\tool-results\definition.json"),
    OSError(5, "写入失败", r"\\target\private\workspace\tool-results\definition.json"),
    RuntimeError("正文冲突: /private/workspace/tool-results/definition.json"),
], ids=["posix", "windows-drive", "windows-unc", "body-conflict"])
@pytest.mark.parametrize("log_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_materialization_failure_is_paired_and_never_forges_reference(
    discovery_case, discovery_store, registry_db, caplog, failure, monkeypatch,
    diagnostic_sink: DiagnosticFailure, log_fails: bool,
):
    tool, catalog, _policies, frozen, read = discovery_case
    original_args = failure.args
    if log_fails:
        diagnostic_sink.primary = failure

    def failed(**_arguments):
        raise failure

    call = {"type": "tool_call", "id": "write-failure", "name": tool.name,
            "args": {"tool_ids": ["huge"], "max_response_bytes": 4096}}
    with monkeypatch.context() as failed_store, caplog.at_level(logging.ERROR, logger="app.services.infrastructure.mcp.tool_search"):
        failed_store.setattr(discovery_store, "materialize", failed)
        message = await tool.ainvoke(call)
    assert message.tool_call_id == "write-failure" and message.status == "error"
    assert message.content == f"definition-materialization-failed: error_type={type(failure).__name__}" + (
        ", diagnostic_sink_error=NotADirectoryError" if log_fails else ""
    )
    assert message.artifact is None
    assert failure.args == original_args
    diagnostic, = [record for record in caplog.records if record.name == "app.services.infrastructure.mcp.tool_search"]
    assert diagnostic.exc_info is None
    assert type(failure).__name__ + ":" in diagnostic.getMessage()
    assert "test_search.py" in diagnostic.getMessage() and "in failed" in diagnostic.getMessage()
    assert str(failure) in caplog.text
    if log_fails:
        sink_error, = diagnostic_sink.errors
        sink_record, = diagnostic_sink.records
        assert sink_record is diagnostic
        assert any(str(sink_error) in note for note in failure.__notes__)
        assert "diagnostic-log-secret" not in message.content
    else:
        assert not diagnostic_sink.errors
    # 显式重试复用原冻结正文，失败不生成可读取引用或改写目录。
    assert restore_frozen_tool_catalog(frozen, read) == catalog
    recovered = await tool.ainvoke(call)
    assert recovered.tool_call_id == "write-failure" and recovered.status == "success"
    entry, = json.loads(recovered.content)["results"]
    assert entry["schema_state"] == "needs_read"
    _saver, session, _connection, _accepted = registry_db
    stored = discovery_store.read(session, entry["read_path"], entry["byte_count"]).data
    assert hashlib.sha256(stored).hexdigest() == entry["content_sha256"]
    definition, = [definition for definition in catalog.definitions if definition.metadata.tool_id == "huge"]
    assert json.loads(stored)["input_schema"] == definition.input_schema


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
    catalog_runtime, prepare_tool_context, registry_db,
):
    owner, factory, _registry, _scope = catalog_runtime
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)

    def catalog(snapshot):
        return restore_frozen_tool_catalog(
            snapshot, lambda frozen, binding: saver.read_resource_activation_payload(frozen, binding, storage_owner=(session, thread)),
        )

    before_wire, before = prepare_tool_context("discovery:model-first", boundary="model_call")
    first = catalog(before)
    factory.session.minimum = 8
    factory.changed()
    await drain_pending_relists(owner)
    after_wire, after = prepare_tool_context("discovery:model-second", boundary="model_call")
    updated = catalog(after)
    old_definition, = (entry for entry in first.definitions if entry.metadata.origin == "mcp")
    new_definition, = (entry for entry in updated.definitions if entry.metadata.origin == "mcp")
    assert old_definition.contract_payload["input_schema"]["properties"]["text"]["minLength"] == 1
    assert new_definition.contract_payload["input_schema"]["properties"]["text"]["minLength"] == 8
    assert old_definition.schema_hash != new_definition.schema_hash
    assert catalog(before) == first
    assert before_wire["tools"] == after_wire["tools"]
    original = saver.get_context_assembly(session, thread, assembly_id=before_wire["assembly_id"])
    current = saver.get_context_assembly(session, thread, assembly_id=after_wire["assembly_id"])
    assert original.seal_binding.prefix.state.epoch == current.seal_binding.prefix.state.epoch
    assert original.request_hash == current.request_hash


@pytest.mark.parametrize("readiness", ["disconnected", "authorization_required"])
async def test_known_unready_definition_is_frozen_without_execution_qualification(
    catalog_runtime, prepare_tool_context, registry_db, discovery_tool_factory, readiness,
):
    owner, _factory, registry, _scope = catalog_runtime
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    _wire, ready = prepare_tool_context("known:ready")
    original_binding = owner.binding_snapshot()
    await owner.disconnect_server("mini", admit=lambda: None)
    if readiness == "authorization_required":
        owner.mark_authorization_required("mini")
    disconnected_binding = owner.binding_snapshot()
    assert {member.member_id for member in disconnected_binding.members} == {"mcp__mini__echo"}
    assert not disconnected_binding.targets and not owner.get_tools()
    with pytest.raises(ExtensionTargetResolutionError, match="连接未就绪"):
        disconnected_binding.resolve("mcp__mini__echo")
    _wire, frozen = prepare_tool_context("known:unready")
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        catalog = restore_frozen_tool_catalog(frozen, lambda snapshot, binding: cold.read_resource_activation_payload(
            snapshot, binding, storage_owner=(session, thread)))
        previous = restore_frozen_tool_catalog(ready, lambda snapshot, binding: cold.read_resource_activation_payload(
            snapshot, binding, storage_owner=(session, thread)))
    definition, = [item for item in catalog.definitions if item.metadata.origin == "mcp"]
    original, = [item for item in previous.definitions if item.metadata.origin == "mcp"]
    assert definition.readiness == readiness and original.readiness == "ready"
    assert definition.contract_payload == original.contract_payload and definition.schema_hash == original.schema_hash
    assert catalog.catalog_revision == previous.catalog_revision
    assert disconnected_binding.binding_hash != original_binding.binding_hash
    resolver = ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={})
    def policies(value):
        return {item.metadata.tool_id: resolver.resolve(item.metadata) for item in value.definitions}
    tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=policies)
    old_tool = discovery_tool_factory(catalog_resolver=lambda: previous, policy_resolver=policies)
    old_cursor = json.loads(await old_tool.ainvoke({"query": "CI", "limit": 1}))["next_cursor"]
    assert old_cursor
    assert "tool-search-snapshot-conflict" in await tool.ainvoke({"query": "CI", "limit": 1, "cursor": old_cursor})
    found, missing = json.loads(await tool.ainvoke({"tool_ids": ["mcp__mini__echo", "never-discovered"]}))["results"]
    assert found["input_schema"] == original.input_schema
    assert found["readiness"] == readiness and found["execution_ready"] is False
    assert found["missing_connections"] == ["mini"]
    assert "连接管理入口" in found["next_step"]
    assert "next_control" not in found  # 本 fixture 没有登记连接控制，不虚构可调用工具。
    assert missing["error"]["code"] == "tool-not-found"
    parent = next(snapshot for snapshot in registry.capture().snapshots if snapshot.facet == "activation")
    assert any(group.get("connection_required_count") == 1 for group in json.loads(parent.payload["content"])["navigation"])
    task_results = json.loads(await tool.ainvoke({"query": "CI 失败"}))["results"]
    assert any(item["tool_id"] == "mcp__mini__echo" and item["readiness"] == readiness for item in task_results)
    for hidden in ("execution_disabled", "model_hidden"):
        restricted = ToolPolicyResolver(policy_defaults={}, policy_rules={}, restrictions={hidden: ["mcp__mini__echo"]})
        restricted_tool = discovery_tool_factory(catalog_resolver=lambda: catalog, policy_resolver=lambda value, restricted=restricted: {
            item.metadata.tool_id: restricted.resolve(item.metadata) for item in value.definitions})
        error, = json.loads(await restricted_tool.ainvoke({"tool_ids": ["mcp__mini__echo"]}))["results"]
        assert error["status"] == "error" and "input_schema" not in error
    # 重连不能让旧目录专用成员取得资格，只有新 publication 获得新连接。
    await owner.connect_server("mini", admit=lambda: None, on_initialized=lambda: None)
    assert owner.binding_snapshot().resolve("mcp__mini__echo").connection_ref != original_binding.resolve("mcp__mini__echo").connection_ref
    assert not disconnected_binding.targets and definition.readiness == readiness
    assert registry.capture().snapshots


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
    discovery_case, catalog_runtime, prepare_tool_context, registry_db, discovery_tool_factory,
    management_catalog,
):
    tool, original, _policies, frozen, read = discovery_case
    _owner, factory, registry, _scope = catalog_runtime
    before = await tool.ainvoke({"tool_ids": ["mcp__mini__echo"]})
    assert "mcp__mini__echo" in {item["id"] for item in management_catalog.list_tool_definitions()[1]}
    query = {"query": "CI", "limit": 1}
    cursor = json.loads(await tool.ainvoke(query))["next_cursor"]
    assert cursor
    continuation = await tool.ainvoke({**query, "cursor": cursor})
    factory.session.minimum = 10
    factory.session.empty = True
    factory.changed()
    await drain_pending_relists(_owner)
    assert "mcp__mini__echo" not in {item["id"] for item in management_catalog.list_tool_definitions()[1]}
    assert await tool.ainvoke({**query, "cursor": cursor}) == continuation
    assert definition_resource_id("mcp__mini__echo") not in {item.resource_id for item in registry.capture().snapshots}
    _wire, same_turn = prepare_tool_context("discovery:same-turn", boundary="turn")
    assert same_turn == frozen
    assert restore_frozen_tool_catalog(same_turn, read) == original
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        restored = restore_frozen_tool_catalog(
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
        restore_frozen_tool_catalog(without_definition, read)


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
