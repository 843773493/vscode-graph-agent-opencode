"""9.5：full_rollout_copy 的 activation target-local identity 合同测试。

复制到新 Session 后必须生成 target-local ``activation_snapshot_id`` 与 assembly
binding，并把 source→target 记入 ``fork_identity_mappings`` 作为 lineage；source
owner 的 operational ref 不得直接用于 target lookup。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.storage.resource_activation_lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
    ResourceActivationStore,
)
from tests.unit.services.infrastructure.rollout_context.test_resource_activation_retention import (
    SESSION_ID,
    _saver,
    _sealed_with_activation,
)

TARGET_SESSION_ID = "ses_8f2c5d1e4a3b46c09e7d5812ab34cd56"


def test_full_copy_rebuilds_target_local_activation_identity(
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
    )

    with closing(sqlite3.connect(saver._storage.index_path(TARGET_SESSION_ID, ""))) as connection:
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
    restarted.attach_resource_activation_store(
        ResourceActivationStore(
            restarted._storage,
            ActivationLineageBodyStore(restarted._storage, restarted._detail_store),
        )
    )
    with closing(sqlite3.connect(restarted._storage.index_path(TARGET_SESSION_ID, ""))) as connection:
        target_assembly = connection.execute(
            "SELECT assembly_id, plan_hash, request_hash FROM context_assemblies LIMIT 1"
        ).fetchone()
    restored = restarted.load_sealed_resource_activation(
        TARGET_SESSION_ID,
        assembly_id=target_assembly[0],
        plan_hash=target_assembly[1],
        request_hash=target_assembly[2],
    )
    assert restored.activation_snapshot_id == target_snapshot_id
    assert restored.owner_session_id == TARGET_SESSION_ID
    assert [b.resource_id for b in restored.bindings] == [
        b.resource_id for b in snapshot.bindings
    ]
