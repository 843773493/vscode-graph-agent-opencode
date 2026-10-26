"""真实 MCP Owner 的完整成员事实经冻结、seal、restart、fork 和 retention 保持。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.lifecycle import LifetimeScope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogResourcePublisher
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.store import (
    lineage_manifest,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
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


@pytest.mark.asyncio
async def test_gc_removes_unreferenced_historical_detail_from_target_node_only(
    registry_db, session_bundle_factory, fork_deadlines,
):
    saver, source_session_id, _connection, _accepted = registry_db
    storage = saver._storage.storage
    source_thread_id = storage.main_thread_id(source_session_id)
    body = {"source": "expired historical detail"}
    record = saver._detail_store.write(
        session_id=source_session_id,
        thread_id=source_thread_id,
        assembly_id="historical-orphan",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail=body,
        retention_days=1,
    )
    saver._storage.assemblies.register_context_plan_detail(record)
    source_node = storage._path_resolver.resolve_thread_node(
        source_session_id, source_thread_id
    )
    source_body = source_node / record.relative_path
    assert source_body.is_file()

    target_session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(storage.sessions_dir, target_session_id)
    await copy_rollout_snapshot(
        saver, source_session_id, target_session_id, fork_deadlines=fork_deadlines,
    )

    with RolloutCheckpointSaver(storage.sessions_dir) as target_saver:
        target_storage = target_saver._storage.storage
        target_thread_id = target_storage.main_thread_id(target_session_id)
        target_node = target_storage._path_resolver.resolve_thread_node(
            target_session_id, target_thread_id
        )
        target_body = target_node / record.relative_path
        assert target_body.is_file()
        assert target_body.read_bytes() == source_body.read_bytes()
        assert record.detail_ref.session_id == source_session_id
        assert (record.detail_ref.session_id, record.detail_ref.thread_id) != (
            target_session_id, target_thread_id,
        )
        assert not target_saver._storage.assemblies.context_assembly_references_detail(
            target_session_id,
            target_thread_id,
            assembly_id=record.detail_ref.assembly_id,
            detail_ref=record.detail_ref,
        )

        expired_before = datetime.now(UTC) + timedelta(days=2)
        candidates = target_saver._storage.assemblies.list_expired_context_plan_details(
            target_session_id, target_thread_id, expired_before=expired_before,
        )
        assert candidates == (record.detail_ref,)
        assert target_saver.gc_context_plan_details(
            target_session_id, target_thread_id, expired_before=expired_before,
        ) == (record.detail_ref,)
        assert not target_body.exists()
        with closing(sqlite3.connect(target_storage.index_path(
            target_session_id, "", thread_id=target_thread_id,
        ))) as connection:
            assert connection.execute(
                "SELECT status, availability FROM context_plan_details WHERE detail_ref = ?",
                (canonical_json_bytes(record.detail_ref.to_dict()).decode("utf-8"),),
            ).fetchone() == ("unavailable", "unavailable")

    assert source_body.is_file()
    assert saver.read_context_plan_detail(
        source_session_id, source_thread_id, detail_ref=record.detail_ref,
    )["detail"] == body


@pytest.fixture
async def catalog_runtime(catalog_workspace, extension_catalog_policy_resolver):
    factory = CatalogSessionFactory()
    owner = McpCatalogOwner(
        leases=ExternalResourceLeaseLedger(),
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
    publisher = McpCatalogResourcePublisher(registry=registry, custom_tools=custom, policy_resolver=extension_catalog_policy_resolver)
    scope = LifetimeScope("members-persistence")
    scope.register(owner.subscribe_publication(publisher.publish), label="mcp-members-publication")
    try:
        yield owner, factory, registry, scope
    finally:
        await scope.close()
        await owner.shutdown()




@pytest.mark.asyncio
async def test_complete_mixed_members_survive_five_real_persistence_paths(
    catalog_runtime, registry_db, guidance_prepare, session_bundle_factory, monkeypatch,
    fork_deadlines,
):
    _owner, factory, registry, _scope = catalog_runtime
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
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
    assert saver.read_resource_activation_payload(
        frozen, parent_row, storage_owner=(session, thread),
    ) == published.payload
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
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        restored = restarted.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        )
        assert restored.to_dict() == frozen.to_dict()
        assert restored.parent.to_dict() == frozen.parent.to_dict()
        session_bundle_factory(restarted._storage.storage.sessions_dir, TARGET_SESSION_ID)
        await copy_rollout_snapshot(
            restarted, session, TARGET_SESSION_ID, fork_deadlines=fork_deadlines,
        )
        target_thread = restarted._storage.storage.main_thread_id(TARGET_SESSION_ID)
        target_path = restarted._storage.storage.index_path(TARGET_SESSION_ID, "", thread_id=target_thread)
        with closing(sqlite3.connect(target_path)) as connection:
            target_assembly = connection.execute(
                "SELECT assembly_id,plan_hash,request_hash FROM context_assemblies WHERE assembly_id=?",
                (assembly.assembly_id,),
            ).fetchone()
        forked = restarted.load_sealed_resource_activation(
            TARGET_SESSION_ID, thread_id=target_thread, assembly_id=target_assembly[0],
            plan_hash=target_assembly[1], request_hash=target_assembly[2],
        )
        assert forked.to_dict() == frozen.to_dict()
        assert forked.parent.to_dict() == frozen.parent.to_dict()
        assert forked.bindings_hash == frozen.bindings_hash
        assert forked.activation_provenance_hash == frozen.activation_provenance_hash
        assert lineage_manifest(forked) == lineage_manifest(frozen)
        for source_binding, target_binding in zip(frozen.bindings, forked.bindings, strict=True):
            assert target_binding.members == source_binding.members
            assert target_binding.source_lineage_ref == source_binding.source_lineage_ref
            assert target_binding.source_lineage_digest == source_binding.source_lineage_digest
            assert restarted.read_resource_activation_payload(
                forked, target_binding, storage_owner=(TARGET_SESSION_ID, target_thread),
            ) == (
                saver.read_resource_activation_payload(
                    frozen, source_binding, storage_owner=(session, thread),
                )
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
        candidates = restarted._storage.assemblies.list_expired_context_plan_details(
            TARGET_SESSION_ID, target_thread,
            expired_before=datetime.now(UTC) + timedelta(days=31),
        )
        candidate_keys = {
            canonical_json_bytes(ref.to_dict()).decode("utf-8") for ref in candidates
        }
        assert set(protected_keys) <= candidate_keys
        with pytest.raises(RuntimeError, match="detail-retention-protected"):
            restarted._storage.assemblies.mark_context_plan_details_unavailable(
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
        assert retained.to_dict() == forked.to_dict()
        assert retained.parent.to_dict() == forked.parent.to_dict()
        assert restarted.load_sealed_resource_activation(
            session, thread_id=thread, assembly_id=assembly.assembly_id,
            plan_hash=assembly.plan_hash, request_hash=assembly.request_hash,
        ).to_dict() == frozen.to_dict()
    assert factory.session.list_calls == before_list_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_before_restore", (False, True), ids=("source", "full-copy"))
async def test_checkpoint_restore_preserves_frozen_mcp_parent_and_members(
    catalog_runtime, registry_db, guidance_prepare, session_bundle_factory, monkeypatch,
    fork_deadlines, copy_before_restore,
):
    _owner, factory, registry, _scope = catalog_runtime
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    published = registry.snapshot(MCP_TOOL_CATALOG_RESOURCE_ID)
    assert published.display_uri is None
    assert len(published.members) == 3
    assert {member.member_kind for member in published.members} == {"mcp", "custom"}
    assert [member.member_id for member in published.members] == sorted(
        member.member_id for member in published.members
    )
    before_list_calls = factory.session.list_calls

    async def forbid_list():
        raise AssertionError("checkpoint 持久恢复不得调用 MCP tools/list")

    monkeypatch.setattr(factory.session, "list_tools", forbid_list)
    prepared, frozen = guidance_prepare("members-checkpoint-restore")
    parent_row = next(
        binding for binding in frozen.bindings
        if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID
    )
    assert parent_row.members == published.members
    assembly = saver.get_context_assembly(
        session, thread, assembly_id=prepared["assembly_id"],
    )
    assert frozen.model_call_id is not None
    saver.register_model_call(
        session, thread_id=thread, execution_id=frozen.execution_id,
        model_call_id=frozen.model_call_id, attempt=1, provider="fixture-model",
        assembly_id=assembly.assembly_id,
    )
    # 使用真实 prepare 投影出的 accepted root，不能构造第二份用户输入。
    user = next(message for message in prepared["messages"] if isinstance(message, HumanMessage))
    assert "item-" + user.id == accepted["root_input_item_id"]
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "members-checkpoint"
    checkpoint["channel_values"] = {"messages": [user]}
    checkpoint["channel_versions"] = {"messages": checkpoint["id"]}
    checkpoint["updated_channels"] = ["messages"]
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        saver.put(
            build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 1},
            checkpoint["channel_versions"],
        )

    def forbid_registry_read(*_args, **_kwargs):
        raise AssertionError("checkpoint 持久恢复不得读取 live Registry")

    monkeypatch.setattr(registry, "capture", forbid_registry_read)
    monkeypatch.setattr(registry, "snapshot", forbid_registry_read)
    target_session = session
    if copy_before_restore:
        target_session = TARGET_SESSION_ID
        session_bundle_factory(saver._storage.storage.sessions_dir, target_session)
        await copy_rollout_snapshot(
            saver, session, target_session, fork_deadlines=fork_deadlines,
        )
        source_root = saver._storage.storage.root(session, thread_id=thread)
        source_root.rename(source_root.with_name("source-offline"))

    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as restarted:
        target_thread = restarted._storage.storage.main_thread_id(target_session)
        config = build_checkpoint_config(target_thread, checkpoint_id=checkpoint["id"])
        with session_thread_owner_scope(
            MutationIntentOwner(target_session, target_thread), langgraph_thread_id=target_thread,
        ):
            restored_checkpoint = restarted.get_tuple(config)
            assert restored_checkpoint is not None
            assert restored_checkpoint.checkpoint["id"] == checkpoint["id"]
            assert restored_checkpoint.checkpoint["channel_versions"] == checkpoint["channel_versions"]
            assert restored_checkpoint.checkpoint["updated_channels"] == checkpoint["updated_channels"]
            assert restored_checkpoint.checkpoint["versions_seen"] == checkpoint["versions_seen"]
            assert restored_checkpoint.checkpoint["pending_sends"] == checkpoint["pending_sends"]
            assert restored_checkpoint.checkpoint["channel_values"].keys() == checkpoint["channel_values"].keys()
            restored_messages = restored_checkpoint.checkpoint["channel_values"]["messages"]
            assert len(restored_messages) == 1
            restored_user = restored_messages[0]
            assert isinstance(restored_user, HumanMessage)
            assert restored_user.id == user.id
            assert restored_user.content == user.content

            # get_tuple 返回 canonical projection，并按 SQLite 权威身份 hydration；
            # 按同一持久 item 的展示 projection 比较正文与 typed provenance。
            target_codec = restarted._storage.storage._codec()
            canonical_root = restarted._storage.storage.read_items(
                target_session, target_thread, item_ids=(accepted["root_input_item_id"],),
            )
            assert len(canonical_root) == 1
            expected_projection = target_codec.project_message(canonical_root)
            expected_projection_data = dict(expected_projection["data"])
            restored_projection = target_codec.to_dict(restored_user)
            restored_projection_data = dict(restored_projection["data"])
            expected_canonical_metadata = dict(expected_projection_data.pop("response_metadata"))
            restored_projection_metadata = dict(restored_projection_data.pop("response_metadata"))
            assert restored_projection["type"] == expected_projection["type"]
            assert restored_projection_data == expected_projection_data
            assert expected_canonical_metadata["provenance_relations"] == [
                relation.to_dict()
                for item in canonical_root
                for relation in item.provenance_relations
            ]
            assert [
                manifest["item_id"]
                for manifest in expected_canonical_metadata["canonical_item_manifests"]
            ] == [item.item_id for item in canonical_root]

            with closing(sqlite3.connect(restarted._storage.storage.index_path(
                target_session, "", thread_id=target_thread,
            ))) as connection:
                sqlite_identity = connection.execute(
                    "SELECT message_id, turn_id, created_at FROM messages WHERE message_id = ?",
                    (user.id,),
                ).fetchone()
            assert sqlite_identity is not None
            assert sqlite_identity[1] is not None
            expected_message_metadata = dict(
                expected_canonical_metadata.pop("message_metadata", {}),
            )
            restored_message_metadata = dict(
                restored_projection_metadata.pop("message_metadata", {}),
            )
            assert restored_message_metadata == {
                **expected_message_metadata,
                "turn_id": sqlite_identity[1],
            }
            assert {
                "message_id": restored_projection_metadata.pop("message_id"),
                "created_at": restored_projection_metadata.pop("created_at"),
                "updated_at": restored_projection_metadata.pop("updated_at"),
            } == {
                "message_id": sqlite_identity[0],
                "created_at": sqlite_identity[2],
                "updated_at": sqlite_identity[2],
            }
            assert restored_projection_metadata == expected_canonical_metadata
            restored_config = restarted.rewind(
                restored_checkpoint.config, checkpoint_id=restored_checkpoint.checkpoint["id"],
            )
            rewound = restarted.get_tuple(restored_config)
            assert rewound is not None
            rewound_messages = rewound.checkpoint["channel_values"]["messages"]
            assert len(rewound_messages) == 1
            assert isinstance(rewound_messages[0], HumanMessage)
            assert target_codec.to_dict(rewound_messages[0]) == restored_projection

    # rewind 提交后重新创建 Saver，后继只消费 control 里已提交的精确引用。
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        with closing(sqlite3.connect(cold._storage.storage.index_path(
            target_session, "", thread_id=target_thread,
        ))) as connection:
            control_rows = connection.execute(
                "SELECT payload_json FROM control_events WHERE control_kind = 'rewind'"
            ).fetchall()
        assert len(control_rows) == 1
        control = json.loads(control_rows[0][0])
        assert control["source_checkpoint_id"] == restored_checkpoint.checkpoint["id"]
        assert control["resource_activation_refs"] == [{
            "assembly_id": assembly.assembly_id,
            "turn_id": frozen.turn_id,
            "activation_snapshot_id": frozen.activation_snapshot_id,
            "plan_hash": assembly.plan_hash,
            "request_hash": assembly.request_hash,
            "bindings_hash": frozen.bindings_hash,
            "activation_provenance_hash": frozen.activation_provenance_hash,
        }]
        ref = control["resource_activation_refs"][0]
        restored = cold.load_sealed_resource_activation(
            target_session, thread_id=target_thread, assembly_id=ref["assembly_id"],
            plan_hash=ref["plan_hash"], request_hash=ref["request_hash"],
        )
        assert restored.activation_snapshot_id == ref["activation_snapshot_id"]
        assert (restored.owner_session_id, restored.owner_thread_id) == (session, thread)
        assert restored.to_dict() == frozen.to_dict()
        assert restored.parent.to_dict() == frozen.parent.to_dict()
        assert restored.bindings_hash == ref["bindings_hash"] == frozen.bindings_hash
        assert restored.activation_provenance_hash == ref["activation_provenance_hash"]
        assert lineage_manifest(restored) == lineage_manifest(frozen)
        restored_parent = next(
            binding for binding in restored.bindings
            if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID
        )
        assert restored_parent.to_dict() == parent_row.to_dict()
        assert [member.to_dict() for member in restored_parent.members] == [
            member.to_dict() for member in published.members
        ]
        assert restored_parent.source_lineage_ref.sources == tuple(sorted(published.source_lineage))
        assert cold.read_resource_activation_payload(
            restored, restored_parent, storage_owner=(target_session, target_thread),
        ) == published.payload
    assert factory.session.list_calls == before_list_calls
