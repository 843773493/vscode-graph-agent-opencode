"""真实语义发布经 factory seal、冷读和 fork 始终保留显式空 URI。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.identifier import create_uuid_hex
from app.core.resource_uri import parse_resource_uri
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)
from tests.integration.backend.sessions.rollout_context.test_agent_production_context import (
    factory_context as factory_context,  # noqa: PLC0414 - 复用真实 factory，工作区按当前测试文件隔离
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - factory fixture 的真实存储依赖
)


@pytest.mark.parametrize("factory_context", ["chat_completions", "responses"], indirect=True)
def test_internal_aggregates_keep_explicit_null_through_seal_restart_and_fork(
    factory_context, session_bundle_factory, fork_deadlines,
):
    saver, session, thread, connection, _selection, _manager, captured, _tools, dispatch, _files = factory_context
    registry = captured["execution"]._resource_activation._registry
    aggregate_ids = {"skill-entry:workspace:catalog:catalog", MCP_TOOL_CATALOG_RESOURCE_ID}
    capture = registry.capture()
    for resource_id in aggregate_ids:
        assert registry.descriptor(resource_id).display_uri is None
        assert registry.snapshot(resource_id).display_uri is None
    assert aggregate_ids <= {descriptor.resource_id for descriptor in capture.descriptors}
    skill_uri = registry.descriptor("skill-entry:workspace:example:activation").display_uri
    assert skill_uri is not None
    parsed = parse_resource_uri(skill_uri)
    workspace_id = saver._storage.storage._path_resolver.catalog_store.get_node(session).workspace_id
    assert (parsed.scope, parsed.scope_id, parsed.kind, parsed.tail_segments, parsed.display_uri) == (
        "workspace", workspace_id, "skills", ("example", "SKILL.md"), skill_uri,
    )
    # 此 fixture 的 MCP servers 为空，仍须发布无 URI 的真实聚合父行。
    assert registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID).members == ()

    dispatch("aggregate-null-uri")
    assembly = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    activation = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=assembly.assembly_id,
        plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
    )
    bindings = {binding.resource_id: binding for binding in activation.bindings}
    for resource_id in aggregate_ids:
        assert bindings[resource_id].to_dict()["display_uri"] is None
    assert bindings["skill-entry:workspace:example:activation"].display_uri == skill_uri
    assert connection.execute(
        "SELECT DISTINCT display_uri FROM resource_activation_bindings WHERE resource_id IN (?, ?)",
        tuple(sorted(aggregate_ids)),
    ).fetchall() == [(None,)]
    original = activation.to_dict()
    payloads = {
        binding.resource_id: saver.read_resource_activation_payload(
            activation, binding, storage_owner=(session, thread),
        )
        for binding in activation.bindings
    }

    sessions = saver._storage.storage.sessions_dir
    target = "ses_" + create_uuid_hex()
    session_bundle_factory(sessions, target)
    target_thread = saver._storage.storage.main_thread_id(target)
    asyncio.run(copy_rollout_snapshot(saver, session, target, fork_deadlines=fork_deadlines))

    source_root = saver._storage.storage.root(session, thread_id=thread)
    source_root.rename(source_root.with_name("source-offline"))
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.load_sealed_resource_activation(
            target, thread_id=target_thread, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        )
        assert restored.to_dict() == original
        assert restored.bindings_hash == activation.bindings_hash
        assert restored.activation_provenance_hash == activation.activation_provenance_hash
        for binding in restored.bindings:
            assert restarted.read_resource_activation_payload(
                restored, binding, storage_owner=(target, target_thread),
            ) == payloads[binding.resource_id]
