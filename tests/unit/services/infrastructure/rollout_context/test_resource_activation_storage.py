"""9.2 activation snapshot catalog 的 SQLite 持久化与迁移合同测试。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_migration import (
    MIGRATION_LOSS_REASON_MISSING_ACTIVATION,
    read_migration_losses,
    upgrade_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
    SCHEMA_UNAVAILABLE_CODE,
    ResourceActivationStore,
    ResourceActivationStoreError,
    lineage_manifest_digest,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

SESSION_ID = "ses_4ac0b0f52f364cc7b90e18a6a6bb0c1f"


def _binding(
    resource_id: str,
    *,
    ordinal: int,
    boundary: str,
    revision: str = "rev-1",
    lineage_revision: str = "src-rev-1",
) -> ResourceProvenanceRef:
    return ResourceProvenanceRef(
        resource_id=resource_id,
        display_uri=f"boxteam://workspace/test/{resource_id.replace(':', '-')}",
        resource_kind="skills",
        owner_scope="workspace",
        facet="activation",
        revision=revision,
        availability="available",
        content_length=7,
        content_hash="sha256:jcs:v1:" + "a" * 64,
        redacted_stable_digest=None,
        source_lineage_ref=SourceLineageRef(
            lineage_id="lineage-1",
            derivation_version="derivation:v1",
            sources=(("source-1", lineage_revision),),
        ),
        source_lineage_digest=SourceLineageRef(
            lineage_id="lineage-1",
            derivation_version="derivation:v1",
            sources=(("source-1", lineage_revision),),
        ).digest,
        activation_ordinal=ordinal,
        effective_boundary=boundary,
        captured_registry_generation=3,
        snapshot_ref=DetailRef(SESSION_ID, "snapshot-body-1", "body-1"),
    )


def _turn_snapshot(
    turn_id: str = "turn-1", model_call_id: str | None = None
) -> ResourceActivationSnapshotRef:
    return ResourceActivationSnapshotRef(
        activation_snapshot_id=f"turn:{SESSION_ID}:main:{turn_id}",
        snapshot_kind="turn",
        activation_policy_revision="resource-activation-policy:v1:abc",
        activation_policy_hash="sha256:jcs:v1:" + "1" * 64,
        registry_generation=3,
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id=turn_id,
        captured_at="2026-09-24T00:00:00+00:00",
        bindings=(_binding("skill:demo", ordinal=0, boundary="turn"),),
        model_call_id=model_call_id,
    )


def _model_call_snapshot(
    parent: ResourceActivationSnapshotRef,
) -> ResourceActivationSnapshotRef:
    return ResourceActivationSnapshotRef(
        activation_snapshot_id=parent.activation_snapshot_id + ":call-1",
        snapshot_kind="model_call",
        activation_policy_revision=parent.activation_policy_revision,
        activation_policy_hash=parent.activation_policy_hash,
        registry_generation=4,
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id=parent.turn_id,
        captured_at="2026-09-24T00:00:01+00:00",
        bindings=(
            parent.bindings[0],
            _binding("mcp:catalog", ordinal=1, boundary="model_call"),
        ),
        parent=parent,
        model_call_id="call-1",
    )


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SESSION_ID)
    return root


@pytest.fixture
def storage(sessions_root: Path) -> RolloutStorage:
    instance = RolloutStorage(sessions_root)
    instance.initialize(SESSION_ID, validate_jsonl_items=False)
    return instance


@pytest.fixture
def activation_store(
    sessions_root: Path, storage: RolloutStorage
) -> ResourceActivationStore:
    detail_store = ContextPlanDetailStore(sessions_root, protected_key=b"k" * 32)
    return ResourceActivationStore(
        storage, ActivationLineageBodyStore(storage, detail_store)
    )


def _bootstrap(store: ResourceActivationStore, storage: RolloutStorage) -> None:
    with storage._lock(SESSION_ID, ""), storage._connect(SESSION_ID, "") as connection:
        connection.execute("BEGIN IMMEDIATE")
        store.bootstrap_schema(connection)
        connection.commit()


def test_save_and_read_round_trip_preserves_identity(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    body_ref = activation_store.save_snapshot(snapshot)

    assert body_ref.session_id == SESSION_ID
    restored = activation_store.read_snapshot(
        SESSION_ID, thread_id="main", activation_snapshot_id=snapshot.activation_snapshot_id
    )

    assert restored.to_dict() == snapshot.to_dict()
    assert restored.bindings_hash == snapshot.bindings_hash
    assert restored.activation_provenance_hash == snapshot.activation_provenance_hash

    # 幂等：相同 identity 再次保存返回同一 lineage ref，不新增行。
    assert activation_store.save_snapshot(snapshot) == body_ref
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM resource_activation_snapshots"
        ).fetchone()[0]
    assert count == 1


def test_model_call_snapshot_reuses_parent_bytes(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    _bootstrap(activation_store, storage)
    turn = _turn_snapshot()
    activation_store.save_snapshot(turn)
    model_call = _model_call_snapshot(turn)
    activation_store.save_snapshot(model_call)

    restored = activation_store.read_snapshot(
        SESSION_ID,
        thread_id="main",
        activation_snapshot_id=model_call.activation_snapshot_id,
    )

    assert restored.parent is not None
    assert restored.parent.to_dict() == turn.to_dict()
    assert restored.bindings[: len(turn.bindings)] == turn.bindings
    assert restored.parent_turn_snapshot_id == turn.activation_snapshot_id


def test_runtime_without_activation_schema_fails_closed(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    # 未 bootstrap 时正常 runtime 不得动态建表或静默读取。
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.save_snapshot(_turn_snapshot())
    assert error.value.code == SCHEMA_UNAVAILABLE_CODE

    with pytest.raises(ResourceActivationStoreError) as read_error:
        activation_store.read_snapshot(
            SESSION_ID, thread_id="main", activation_snapshot_id="turn:missing"
        )
    assert read_error.value.code == SCHEMA_UNAVAILABLE_CODE

    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "resource_activation_snapshots" not in tables


def test_unregistered_schema_version_fails_closed(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    _bootstrap(activation_store, storage)
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_schema_state SET activation_schema_version = 99"
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID, thread_id="main", activation_snapshot_id="turn:any"
        )
    assert error.value.code == SCHEMA_UNAVAILABLE_CODE


def test_tampered_binding_digest_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_bindings SET content_hash = ?",
            ("sha256:jcs:v1:" + "b" * 64,),
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id="main",
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_tampered_lineage_digest_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    """binding 列的 lineage digest 必须与受保护 manifest 逐字节一致。"""

    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_bindings SET source_lineage_digest = ?",
            ("sha256:jcs:v1:" + "c" * 64,),
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id="main",
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_conflicting_snapshot_identity_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    """同一 activation_snapshot_id 不得被不同 binding 选择覆盖。"""

    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    activation_store.save_snapshot(snapshot)
    conflicting = ResourceActivationSnapshotRef(
        activation_snapshot_id=snapshot.activation_snapshot_id,
        snapshot_kind="turn",
        activation_policy_revision=snapshot.activation_policy_revision,
        activation_policy_hash=snapshot.activation_policy_hash,
        registry_generation=snapshot.registry_generation,
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id=snapshot.turn_id,
        captured_at=snapshot.captured_at,
        bindings=(
            _binding(
                "skill:demo", ordinal=0, boundary="turn", revision="rev-2"
            ),
        ),
    )
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.save_snapshot(conflicting)
    assert error.value.code == "resource-activation-snapshot-conflict"


def test_tampered_catalog_lineage_digest_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    """catalog 的 lineage manifest digest 必须与受保护正文重算一致。"""

    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_snapshots SET lineage_manifest_digest = ?",
            ("sha256:jcs:v1:" + "e" * 64,),
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id="main",
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_tampered_binding_count_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    """snapshot 的 binding_count 必须与实际 binding 行数一致。"""

    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_snapshots SET binding_count = 7"
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id="main",
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_lineage_manifest_is_protected_and_absent_from_catalog(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    activation_store.save_snapshot(snapshot)

    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(resource_activation_bindings)"
            )
        }
    # catalog 只有 digest；source 向量不进 SQLite。
    assert "source_lineage_digest" in columns
    assert "sources" not in columns
    assert "source_lineage_ref" not in columns
    # lineage manifest digest 覆盖全部 binding 的来源向量，不等于单条 binding digest。
    assert lineage_manifest_digest(snapshot) != snapshot.bindings[0].source_lineage_digest
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        stored_digest = connection.execute(
            "SELECT lineage_manifest_digest FROM resource_activation_snapshots"
        ).fetchone()[0]
    assert stored_digest == lineage_manifest_digest(snapshot)


def test_bind_assembly_requires_committed_snapshot(
    activation_store: ResourceActivationStore, storage: RolloutStorage
) -> None:
    _bootstrap(activation_store, storage)
    snapshot = _turn_snapshot()
    with (
        pytest.raises(ResourceActivationStoreError) as error,
        storage._lock(SESSION_ID, ""),
        storage._connect(SESSION_ID, "") as connection,
    ):
        connection.execute("BEGIN IMMEDIATE")
        activation_store.bind_assembly(
            connection,
            snapshot=snapshot,
            assembly_id="assembly-1",
            plan_id="plan-1",
            plan_hash="plan-hash",
            request_hash="request-hash",
            selection_manifest_hash="selection-hash",
        )
    assert error.value.code == "resource-activation-hash-mismatch"

    activation_store.save_snapshot(snapshot)
    with storage._lock(SESSION_ID, ""), storage._connect(SESSION_ID, "") as connection:
        connection.execute("BEGIN IMMEDIATE")
        activation_store.bind_assembly(
            connection,
            snapshot=snapshot,
            assembly_id="assembly-1",
            plan_id="plan-1",
            plan_hash="plan-hash",
            request_hash="request-hash",
            selection_manifest_hash="selection-hash",
        )
        connection.commit()

    binding = activation_store.read_assembly_binding(
        SESSION_ID, assembly_id="assembly-1"
    )
    assert binding["activation_snapshot_id"] == snapshot.activation_snapshot_id
    assert binding["snapshot"].to_dict() == snapshot.to_dict()


def test_migration_quarantines_existing_assemblies_without_fabricating(
    sessions_root: Path,
) -> None:
    storage = RolloutStorage(sessions_root)
    storage.initialize(SESSION_ID, validate_jsonl_items=False)
    with storage._connect(SESSION_ID, "") as connection:
        connection.execute(
            "INSERT INTO context_assemblies(assembly_id, session_id, turn_id, "
            "execution_id, plan_id, plan_hash, request_hash, history_view_revision, "
            "source_overlay_epoch, snapshot_json, model_call_id, status, detail_ref, "
            "created_at, sealed_at) VALUES ('assembly-legacy', ?, 'turn-legacy', "
            "'exec-legacy', 'plan-legacy', 'ph', 'rh', 0, 0, '{}', NULL, 'sealed', "
            "NULL, '2026-09-24T00:00:00+00:00', '2026-09-24T00:00:00+00:00')",
            (SESSION_ID,),
        )
        connection.commit()

    detail_store = ContextPlanDetailStore(sessions_root, protected_key=b"k" * 32)
    ResourceActivationStore(storage, ActivationLineageBodyStore(storage, detail_store))

    with storage._lock(SESSION_ID, ""), storage._connect(SESSION_ID, "") as connection:
        connection.execute("BEGIN IMMEDIATE")
        version, loss_ids = upgrade_resource_activation_schema(connection)
        connection.commit()

    assert version == 1
    assert len(loss_ids) == 1
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        connection.row_factory = sqlite3.Row
        losses = read_migration_losses(connection, session_id=SESSION_ID)
        fabricated = connection.execute(
            "SELECT COUNT(*) FROM resource_activation_snapshots"
        ).fetchone()[0]
    assert fabricated == 0
    assert len(losses) == 1
    assert losses[0]["reason_code"] == MIGRATION_LOSS_REASON_MISSING_ACTIVATION
    assert losses[0]["assembly_id"] == "assembly-legacy"
    assert json.loads(json.dumps(losses[0]["detail"]))["assembly_id"] == "assembly-legacy"

    # 迁移后既有 assembly 内容仍可读；marker 已就位。
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        row = connection.execute(
            "SELECT plan_hash FROM context_assemblies WHERE assembly_id = 'assembly-legacy'"
        ).fetchone()
        version_row = connection.execute(
            "SELECT activation_schema_version FROM resource_activation_schema_state"
        ).fetchone()
    assert row == ("ph",)
    assert version_row == (1,)

    # 幂等：再次迁移不重复建表、不新增 loss。
    with storage._lock(SESSION_ID, ""), storage._connect(SESSION_ID, "") as connection:
        connection.execute("BEGIN IMMEDIATE")
        again_version, again_losses = upgrade_resource_activation_schema(connection)
        connection.commit()
    assert (again_version, again_losses) == (1, ())
    with closing(sqlite3.connect(storage.index_path(SESSION_ID, ""))) as connection:
        total_losses = connection.execute(
            "SELECT COUNT(*) FROM resource_activation_migration_losses"
        ).fetchone()[0]
    assert total_losses == 1


def test_store_requires_lineage_body_protection(sessions_root: Path) -> None:
    storage = RolloutStorage(sessions_root)
    storage.initialize(SESSION_ID, validate_jsonl_items=False)
    unprotected = ContextPlanDetailStore(sessions_root)
    store = ResourceActivationStore(
        storage, ActivationLineageBodyStore(storage, unprotected)
    )
    with storage._lock(SESSION_ID, ""), storage._connect(SESSION_ID, "") as connection:
        connection.execute("BEGIN IMMEDIATE")
        store.bootstrap_schema(connection)
        connection.commit()
    with pytest.raises(RuntimeError, match="protected detail backend"):
        store.save_snapshot(_turn_snapshot())
