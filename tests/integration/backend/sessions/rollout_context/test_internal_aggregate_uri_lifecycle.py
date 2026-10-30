"""真实语义发布经 factory seal、冷读和 fork 始终保留显式空 URI。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
from contextlib import closing

import pytest

from app.core.identifier import create_uuid_hex
from app.core.resource_uri import parse_resource_uri
from app.core.sqlite_state import sqlite_file_uri
from app.domain.itemized.resource_activation import ResourceActivationContractError
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
)
from app.services.infrastructure.rollout_context.activation.common import (
    ResourceActivationStoreError,
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
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 正式Session/消息与model_stream生成冻结输入
)
from tests.support.api_helpers import create_session


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


async def test_product_activation_cold_read_rejects_raw_locations_without_republishing_them(
    source_case: RuntimeSourceCase,
) -> None:
    case = source_case
    session = await create_session(case.client, "资源激活拒绝与诊断")
    thread = await case.container.session_service.resolve_main_thread(session)
    await case.plan(session, [{"content": "已封存真实资源。"}, {"content": "原历史仍可续接。"}])
    first = await _send(case, session, "生成真实资源激活历史。")
    assert isinstance(first["job_id"], str)
    await _job(case, first["job_id"])
    saver = case.container.checkpointer
    assembly = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    original = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=assembly.assembly_id,
        plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
    )
    binding = next(binding for binding in original.bindings if binding.display_uri is not None)
    storage = saver._storage.storage
    canonical = storage.jsonl_path(session, thread_id=thread)
    original_bytes = canonical.read_bytes()
    rejections = []
    with closing(sqlite3.connect(sqlite_file_uri(storage.index_path(session, "", thread_id=thread)), uri=True)) as connection:
        policy_json, = connection.execute(
            "SELECT activation_policy_json FROM resource_activation_snapshots WHERE activation_snapshot_id=?",
            (original.activation_snapshot_id,),
        ).fetchone()
        policy = json.loads(policy_json)
        unknown_key = "/private/activation/unknown-field.json"
        cases = [("display_uri", path, path) for path in (
            "/private/activation/source.json", r"C:\private\activation\source.json", r"\\target\private\source.json",
        )] + [("activation_policy_json", json.dumps({**policy, unknown_key: True}), unknown_key)]
        for field, invalid, rejected in cases:
            if field == "display_uri":
                statement = "UPDATE resource_activation_bindings SET display_uri=? WHERE activation_snapshot_id=? AND resource_id=?"
                arguments = (original.activation_snapshot_id, binding.resource_id)
                previous = binding.display_uri
                expected_code = "resource-activation-absolute-path-rejected"
            else:
                statement = "UPDATE resource_activation_snapshots SET activation_policy_json=? WHERE activation_snapshot_id=?"
                arguments = (original.activation_snapshot_id,)
                previous = policy_json
                expected_code = "resource-activation-hash-mismatch"
            connection.execute(statement, (invalid, *arguments))
            connection.commit()
            rows = tuple(connection.execute("SELECT * FROM resource_activation_bindings ORDER BY activation_snapshot_id, activation_ordinal"))
            snapshots = tuple(connection.execute("SELECT * FROM resource_activation_snapshots ORDER BY activation_snapshot_id"))
            try:
                # 损坏只注入真实封存后的索引；关键历史由产品入口生成，冷读不替它修复。
                with (
                    RolloutCheckpointSaver(storage.sessions_dir) as cold,
                    pytest.raises((ResourceActivationContractError, ResourceActivationStoreError)) as caught,
                ):
                    cold.load_sealed_resource_activation(
                        session, thread_id=thread, assembly_id=assembly.assembly_id,
                        plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
                    )
                error = caught.value
                assert error.code == expected_code
                assert rejected not in str(error) and repr(rejected) not in str(error)
                assert repr(rejected) in "".join(traceback.format_exception(error))
                assert tuple(connection.execute("SELECT * FROM resource_activation_bindings ORDER BY activation_snapshot_id, activation_ordinal")) == rows
                assert tuple(connection.execute("SELECT * FROM resource_activation_snapshots ORDER BY activation_snapshot_id")) == snapshots
                assert canonical.read_bytes() == original_bytes
                rejections.append({"field": field, "code": error.code, "message": str(error)})
            finally:
                connection.execute(statement, (previous, *arguments))
                connection.commit()
    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        restored = cold.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        )
        assert restored.to_dict() == original.to_dict()
    second = await _send(case, session, "按原历史继续执行。")
    assert isinstance(second["job_id"], str)
    await _job(case, second["job_id"])
    assert len(case.requests()) == 2
    case.write_actual("activation-rejection", {
        "session_id": session, "thread_id": thread, "assembly_id": assembly.assembly_id,
        "plan_hash": assembly.plan_hash, "request_hash": assembly.request_hash, "rejections": rejections,
        "scope": "真实Session/消息ASGI与model_stream生成历史，封存索引故障后的新Saver冷读；不是Gateway/Web、独立Backend重启或执行失败持久化验收",
    })
