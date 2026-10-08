"""真实 MCP Owner 的完整成员事实经冻结、seal、restart、fork 和 retention 保持。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.tools import StructuredTool

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogResourcePublisher
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork.full_copy.operation import (
    full_rollout_copy,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
    lineage_manifest,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 正式 SQLite fixture 按当前测试路径隔离
)
from tests.unit.services.infrastructure.mcp.test_catalog_activation import (
    CatalogSessionFactory,
)
from tests.unit.services.infrastructure.mcp.test_catalog_activation import (
    catalog_workspace as catalog_workspace,  # noqa: PLC0414 - 完整正式工作区 fixture
)
from tests.unit.services.infrastructure.mcp.test_catalog_activation import (
    guidance_prepare as guidance_prepare,  # noqa: PLC0414 - 复用实际 freeze/seal owner
)

TARGET_SESSION_ID = "ses_019c49da91667f7b842224c1bfc58b01"


@pytest.fixture
async def catalog_runtime(catalog_workspace):
    factory = CatalogSessionFactory()
    owner = McpCatalogOwner(
        raw_config={"servers":{"mini":{"enabled":True, "transport":"stdio", "command":"controlled-server"}}},
        workspace_root=catalog_workspace, event_service=EventChannelService(), session_factory=factory,
    )

    def echo(text: str) -> str:
        return text

    custom = tuple(
        StructuredTool.from_function(echo, name=name, description="真实 custom 成员")
        for name in ("z_custom", "a_custom")
    )
    await owner.start()
    registry = ResourceRegistry(
        publication_lock=owner.publication_lock, capture_validator=owner.validate_resource_capture,
    )
    publisher = McpCatalogResourcePublisher(registry=registry, custom_tools=custom)
    scope = LifetimeScope("members-persistence")
    scope.register(owner.subscribe_publication(publisher.publish), label="mcp-members-publication")
    try:
        yield owner, factory, registry, scope
    finally:
        await scope.close()
        await owner.shutdown()


def _mapped_snapshot(
    source: ResourceActivationSnapshotRef,
    *, target_thread: str,
    mappings: dict[tuple[str, str], str],
) -> dict[str, object]:
    """仅平移原 owner 已登记的 operational refs，其余完整字段必须精确保留。"""
    expected = source.to_dict()
    expected.update({
        "owner_session_id":TARGET_SESSION_ID,
        "owner_thread_id":target_thread,
        "activation_snapshot_id":mappings["activation_snapshot", source.activation_snapshot_id],
        "execution_id":mappings["execution", source.execution_id],
        "turn_id":None if source.turn_id is None else mappings["turn", source.turn_id],
        "model_call_id":None if source.model_call_id is None else mappings["model_call", source.model_call_id],
        "parent_execution_snapshot_id":None if source.parent is None else mappings[
            "activation_snapshot", source.parent.activation_snapshot_id
        ],
    })
    for binding in expected["bindings"]:
        for field in ("snapshot_ref", "detail_ref"):
            if binding[field] is not None:
                source_key = canonical_json_bytes(binding[field]).decode("utf-8")
                binding[field] = json.loads(mappings["detail", source_key])
    return expected


@pytest.mark.asyncio
async def test_complete_mixed_members_survive_five_real_persistence_paths(
    catalog_runtime, registry_db, guidance_prepare, session_bundle_factory, monkeypatch,
):
    _owner, factory, registry, _scope = catalog_runtime
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.main_thread_id(session)
    published = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    assert published.display_uri is None
    assert len(published.members) == 3
    assert {member.member_kind for member in published.members} == {"mcp", "custom"}
    assert [member.member_id for member in published.members] == sorted(
        member.member_id for member in published.members
    )
    assert any(member.server_id == "mini" for member in published.members)
    assert all(member.revision and member.schema_hash for member in published.members)
    before_list_calls = factory.session.list_calls

    async def forbid_list():
        raise AssertionError("冻结、before-model 和持久恢复不得调用 MCP tools/list")

    monkeypatch.setattr(factory.session, "list_tools", forbid_list)
    result, frozen = guidance_prepare("members-five-paths")
    parent_row = next(binding for binding in frozen.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    assert parent_row.display_uri is None and parent_row.members == published.members
    assert [member.to_dict() for member in parent_row.members] == [
        member.to_dict() for member in published.members
    ]
    assert parent_row.source_lineage_ref.sources == tuple(sorted(published.source_lineage))
    assert saver.read_resource_activation_payload(frozen, parent_row) == published.payload
    assembly = saver.get_context_assembly(session, thread, assembly_id=result["assembly_id"])
    sealed = saver.load_sealed_resource_activation(
        session, thread_id=thread, assembly_id=assembly.assembly_id,
        plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
    )
    assert sealed.to_dict() == frozen.to_dict()
    assert sealed.parent.to_dict() == frozen.parent.to_dict()
    assert frozen.model_call_id is not None
    # fork 只平移原 model-call owner 已登记的调用，seal 本身不构造发送事实。
    saver.register_model_call(
        session, thread_id=thread, execution_id=frozen.execution_id,
        model_call_id=frozen.model_call_id, attempt=1, provider="fixture-model",
        assembly_id=assembly.assembly_id,
    )

    def forbid_capture():
        raise AssertionError("sealed retry、restart 和 fork 不得读取 live Registry")

    monkeypatch.setattr(registry, "capture", forbid_capture)
    retry, retry_ref = guidance_prepare("members-five-paths")
    assert retry["messages"] == result["messages"]
    assert retry_ref.to_dict() == frozen.to_dict()
    with RolloutCheckpointSaver(saver._storage.sessions_dir) as restarted:
        restored = restarted.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        )
        assert restored.to_dict() == frozen.to_dict()
        assert restored.parent.to_dict() == frozen.parent.to_dict()
        session_bundle_factory(restarted._storage.sessions_dir, TARGET_SESSION_ID)
        _view, fork_id = full_rollout_copy(
            restarted._storage, source_session_id=session, target_session_id=TARGET_SESSION_ID,
            source_checkpoint_id=None, relationship="detached", checkpoint_ns="",
            detail_capability=restarted._detail_store.fork_detail_capability(),
            source_snapshot_sha256=hashlib.sha256(canonical_json_bytes(frozen.to_dict())).hexdigest(),
        )
        target_thread = restarted._storage.main_thread_id(TARGET_SESSION_ID)
        target_path = restarted._storage.index_path(TARGET_SESSION_ID, "", thread_id=target_thread)
        with closing(sqlite3.connect(target_path)) as connection:
            mappings = {
                (kind, source):target
                for kind, source, target in connection.execute(
                    "SELECT entity_type,source_local_id,target_local_id FROM fork_identity_mappings WHERE fork_id=?",
                    (fork_id,),
                )
            }
            target_assembly = connection.execute(
                "SELECT assembly_id,plan_hash,request_hash FROM context_assemblies WHERE assembly_id=?",
                (mappings["assembly", assembly.assembly_id],),
            ).fetchone()
        forked = restarted.load_sealed_resource_activation(
            TARGET_SESSION_ID, thread_id=target_thread, assembly_id=target_assembly[0],
            plan_hash=target_assembly[1], request_hash=target_assembly[2],
        )
        expected = _mapped_snapshot(frozen, target_thread=target_thread, mappings=mappings)
        assert forked.to_dict() == expected
        assert forked.parent.to_dict() == _mapped_snapshot(
            frozen.parent, target_thread=target_thread, mappings=mappings,
        )
        assert forked.bindings_hash == frozen.bindings_hash
        assert forked.activation_provenance_hash == frozen.activation_provenance_hash
        assert lineage_manifest(forked) == lineage_manifest(frozen)
        for source_binding, target_binding in zip(frozen.bindings, forked.bindings, strict=True):
            assert target_binding.members == source_binding.members
            assert target_binding.source_lineage_ref == source_binding.source_lineage_ref
            assert target_binding.source_lineage_digest == source_binding.source_lineage_digest
            assert restarted.read_resource_activation_payload(forked, target_binding) == (
                saver.read_resource_activation_payload(frozen, source_binding)
            )

        with closing(sqlite3.connect(target_path)) as connection:
            protected_keys = tuple(row[0] for row in connection.execute(
                "SELECT lineage_detail_ref FROM resource_activation_snapshots"
            ))
            protected_keys += tuple(
                canonical_json_bytes((binding.detail_ref or binding.snapshot_ref).to_dict()).decode("utf-8")
                for binding in forked.bindings
            )
            placeholders = ",".join("?" for _ in protected_keys)
        # 以未来 GC cutoff 取得原 manifest 一致的候选，不篡改不可变正文的到期字段。
        candidates = restarted._storage.list_expired_context_plan_details(
            TARGET_SESSION_ID, target_thread,
            expired_before=datetime.now(UTC) + timedelta(days=31),
        )
        candidate_keys = {
            canonical_json_bytes(ref.to_dict()).decode("utf-8") for ref, _assembly in candidates
        }
        assert set(protected_keys) <= candidate_keys
        with pytest.raises(RuntimeError, match="detail-retention-protected"):
            restarted._storage.mark_context_plan_details_unavailable(
                TARGET_SESSION_ID, target_thread,
                detail_refs=tuple(DetailRef.from_dict(json.loads(key)) for key in protected_keys),
            )
        with closing(sqlite3.connect(target_path)) as connection:
            assert set(connection.execute(
                f"SELECT status,availability FROM context_plan_details WHERE detail_ref IN ({placeholders})",
                protected_keys,
            )) == {("available", "available")}
        retained = restarted.load_sealed_resource_activation(
            TARGET_SESSION_ID, thread_id=target_thread, assembly_id=target_assembly[0],
            plan_hash=target_assembly[1], request_hash=target_assembly[2],
        )
        assert retained.to_dict() == expected
        assert retained.parent.to_dict() == forked.parent.to_dict()
        assert restarted.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        ).to_dict() == frozen.to_dict()
    assert factory.session.list_calls == before_list_calls
