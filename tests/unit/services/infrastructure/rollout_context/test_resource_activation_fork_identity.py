"""9.5：full_rollout_copy 的 activation target-local identity 合同测试。

复制到新 Session 后必须生成 target-local ``activation_snapshot_id`` 与 assembly
binding，并把 source→target 记入 ``fork_identity_mappings`` 作为 lineage；source
owner 的 operational ref 不得直接用于 target lookup。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_body import (
    ResourceActivationBodyStore,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from tests.unit.services.infrastructure.rollout_context.test_resource_activation_retention import (
    SESSION_ID,
    _saver,
    _sealed_with_activation,
)

TARGET_SESSION_ID = "ses_019c49da91667f7b842224c1bfc58b01"


@pytest.mark.asyncio
async def test_full_copy_rebuilds_target_local_activation_identity(
    tmp_path: Path, session_bundle_factory
) -> None:
    """full_rollout_copy 必须重造 target-local activation id 并保留 source lineage。"""
    from app.services.infrastructure.rollout_context.fork.full_copy.operation import (
        full_rollout_copy,
    )

    sessions_dir = tmp_path / "sessions"
    for session in (SESSION_ID, TARGET_SESSION_ID):
        session_bundle_factory(sessions_dir, session)
    saver, snapshot, sealed = _sealed_with_activation(sessions_dir)

    _view, _fork_id = full_rollout_copy(
        saver._storage,
        source_session_id=SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
        source_checkpoint_id=None,
        relationship="detached",
        checkpoint_ns="",
        detail_capability=saver._detail_store.fork_detail_capability(),
        source_snapshot_sha256=hashlib.sha256(
            b"synthetic-source-snapshot:resource-activation"
        ).hexdigest(),
    )

    target_thread_id = saver._storage.main_thread_id(TARGET_SESSION_ID)
    with closing(
        sqlite3.connect(
            saver._storage.index_path(
                TARGET_SESSION_ID, "", thread_id=target_thread_id
            )
        )
    ) as connection:
        target_snapshot_id = connection.execute(
            "SELECT activation_snapshot_id FROM resource_activation_snapshots"
        ).fetchone()[0]
        target_session = connection.execute(
            "SELECT session_id FROM resource_activation_snapshots"
        ).fetchone()[0]
        target_assembly_ids = {
            row[0]
            for row in connection.execute(
                "SELECT assembly_id FROM resource_activation_assembly_bindings"
            )
        }
        target_detail_keys = {
            row[0]
            for row in connection.execute(
                "SELECT detail_ref FROM context_plan_details "
                "WHERE detail_kind = 'resource_activation_lineage'"
            )
        }
        lineage_rows = connection.execute(
            "SELECT source_local_id, target_local_id "
            "FROM fork_identity_mappings WHERE entity_type = 'activation_snapshot'"
        ).fetchall()

    # target-local operational identity：不再是 source owner 的裸 ID。
    assert target_session == TARGET_SESSION_ID
    assert target_snapshot_id != snapshot.activation_snapshot_id
    assert snapshot.activation_snapshot_id not in target_snapshot_id
    assert target_snapshot_id.startswith("fork-activation_snapshot:")
    # assembly binding 指向 target-local activation 与 target assembly。
    assert sealed.assembly_id not in target_assembly_ids
    assert all("fork-" in assembly_id for assembly_id in target_assembly_ids)
    # lineage detail 的 assembly 段是 target-local activation snapshot id。
    target_detail_refs = [
        DetailRef.from_dict(json.loads(key)) for key in target_detail_keys
    ]
    assert target_detail_refs
    assert all(
        ref.session_id == TARGET_SESSION_ID
        and ref.assembly_id.startswith("fork-activation_snapshot:")
        for ref in target_detail_refs
    )
    # source→target mapping 记入 fork lineage。
    assert lineage_rows == [(snapshot.activation_snapshot_id, target_snapshot_id)]

    # target 侧按 sealed ref 读回，且与 source 语义等价。
    restarted = _saver(sessions_dir)
    target_thread_id = restarted._storage.main_thread_id(TARGET_SESSION_ID)
    with closing(
        sqlite3.connect(
            restarted._storage.index_path(
                TARGET_SESSION_ID, "", thread_id=target_thread_id
            )
        )
    ) as connection:
        target_assembly = connection.execute(
            "SELECT assembly_id, plan_hash, request_hash FROM context_assemblies LIMIT 1"
        ).fetchone()
    restored = restarted.load_sealed_resource_activation(
        TARGET_SESSION_ID,
        thread_id=restarted._storage.main_thread_id(TARGET_SESSION_ID),
        assembly_id=target_assembly[0],
        plan_hash=target_assembly[1],
        request_hash=target_assembly[2],
    )
    assert restored.activation_snapshot_id == target_snapshot_id
    assert restored.owner_session_id == TARGET_SESSION_ID
    assert [b.members for b in restored.bindings] == [b.members for b in snapshot.bindings]
    assert restored.activation_policy == snapshot.activation_policy
    assert [b.resource_id for b in restored.bindings] == [
        b.resource_id for b in snapshot.bindings
    ]


    # target parent 的 operational ID 已重造，但 lineage 内容 hash 保持语义中立。
    # 新 model-call save 会递归复用 parent，不能因为 source audit ID 留在正文而失败。
    registry = ResourceRegistry()
    descriptor = SemanticResourceDescriptor(
        resource_id="mcp:catalog", resource_kind="mcp_tool_catalog", facet="activation",
        display_uri="boxteam://workspace/test/mcp-catalog",
    )
    registry.register_descriptor(descriptor)
    registry.publish(ResourceSnapshot(
        resource_id=descriptor.resource_id, resource_kind=descriptor.resource_kind,
        facet=descriptor.facet, display_uri=descriptor.display_uri, revision="catalog-target-1",
        payload={"members": [member.to_dict() for member in restored.bindings[0].members]},
        source_lineage=(("mcp-source", "catalog-target-1"),), generation=4,
    ))
    coordinator = ResourceActivationCoordinator(
        registry=registry, saver=restarted,
        body_store=ResourceActivationBodyStore(restarted._storage, restarted._detail_store),
    )
    child = await coordinator.prepare_model_call(parent=restored, model_call_id="fork-target-call")
    assert child.snapshot_kind == "model_call"
    assert child.bindings[:len(restored.bindings)] == restored.bindings
    store = restarted._activation_store()
    parent_ref = store.save_snapshot(restored)
    child_ref = store.save_snapshot(child)
    assert parent_ref.session_id == child_ref.session_id == TARGET_SESSION_ID
    assert parent_ref.thread_id == child_ref.thread_id == target_thread_id
    restored_child = store.read_snapshot(
        TARGET_SESSION_ID, thread_id=target_thread_id,
        activation_snapshot_id=child.activation_snapshot_id,
    )
    assert restored_child.to_dict() == child.to_dict()
    assert restored_child.parent.to_dict() == restored.to_dict()
    assert restored_child.bindings[:len(restored.bindings)] == restored.bindings
    assert store.read_snapshot(
        TARGET_SESSION_ID, thread_id=target_thread_id,
        activation_snapshot_id=restored.activation_snapshot_id,
    ).to_dict() == restored.to_dict()
