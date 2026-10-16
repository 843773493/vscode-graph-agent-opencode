"""9.2 activation snapshot catalog 的 SQLite 持久化合同测试。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationPolicySnapshot,
    ResourceActivationSnapshotRef,
    ResourceMemberBinding,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.infrastructure.rollout_context.activation.lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.activation.schema import (
    RESOURCE_ACTIVATION_SCHEMA_VERSION,
    RESOURCE_ACTIVATION_TABLES,
)
from app.services.infrastructure.rollout_context.activation.store import (
    SCHEMA_UNAVAILABLE_CODE,
    ResourceActivationStore,
    ResourceActivationStoreError,
    lineage_manifest,
    lineage_manifest_digest,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

POLICY = ResourceActivationPolicySnapshot.from_dict({"default_boundary": "turn", "overrides": {"mcp_tool_catalog": "model_call"}})

SESSION_ID = "ses_019c13b18bcc7288beace1cd3b7c4c84"


def _binding(
    resource_id: str,
    *,
    storage: RolloutContextStore,
    ordinal: int,
    boundary: str,
    revision: str = "rev-1",
    lineage_revision: str = "src-rev-1",
) -> ResourceProvenanceRef:
    return ResourceProvenanceRef(
        resource_id=resource_id,
        display_uri=None if boundary == "model_call" else f"boxteam://workspace/test/resources/skills/{resource_id.replace(':', '-')}/SKILL.md",
        resource_kind="mcp_tool_catalog" if boundary == "model_call" else "skills",
        owner_scope="workspace",
        facet="activation",
        revision=revision,
        availability="available",
        content_length=7,
        content_hash="sha256:jcs:v1:" + "a" * 64,
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
        snapshot_ref=DetailRef(
            SESSION_ID, storage.storage.main_thread_id(SESSION_ID), "snapshot-body-1", "body-1"
        ),
    )


def _execution_snapshot(
    storage: RolloutContextStore,
    turn_id: str = "turn-1", model_call_id: str | None = None
) -> ResourceActivationSnapshotRef:
    execution_id = f"execution-{turn_id}"
    storage.executions.accept_turn(
        SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID),
        accepted_ingress_id=f"evt-{turn_id}", acceptance_idempotency_key=f"accept-{turn_id}",
        job_id=f"job-{turn_id}", admission_idempotency_key=f"admit-{turn_id}", payload="问题",
        turn_id=turn_id, root_item_id=f"item-root-{turn_id}", initial_execution_id=execution_id,
    )
    return ResourceActivationSnapshotRef(
        activation_snapshot_id=f"execution:{SESSION_ID}:{storage.storage.main_thread_id(SESSION_ID)}:{turn_id}",
        snapshot_kind="execution",
        activation_policy_revision=POLICY.revision,
        activation_policy_hash=POLICY.hash,
        activation_policy=POLICY,
        registry_generation=3,
        owner_session_id=SESSION_ID,
        owner_thread_id=storage.storage.main_thread_id(SESSION_ID),
        execution_id=execution_id,
        turn_id=turn_id,
        captured_at="2026-09-24T00:00:00+00:00",
        bindings=(_binding("skill:demo", ordinal=0, boundary="turn", storage=storage),),
        model_call_id=model_call_id,
    )


def _model_call_snapshot(
    parent: ResourceActivationSnapshotRef,
    storage: RolloutContextStore,
) -> ResourceActivationSnapshotRef:
    return ResourceActivationSnapshotRef(
        activation_snapshot_id=parent.activation_snapshot_id + ":call-1",
        snapshot_kind="model_call",
        activation_policy_revision=parent.activation_policy_revision,
        activation_policy_hash=parent.activation_policy_hash,
        activation_policy=parent.activation_policy,
        registry_generation=4,
        owner_session_id=SESSION_ID,
        owner_thread_id=storage.storage.main_thread_id(SESSION_ID),
        execution_id=parent.execution_id,
        turn_id=parent.turn_id,
        captured_at="2026-09-24T00:00:01+00:00",
        bindings=(
            parent.bindings[0],
            _binding("mcp:catalog", ordinal=1, boundary="model_call", storage=storage),
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
def storage(sessions_root: Path) -> RolloutContextStore:
    instance = RolloutContextStore(sessions_root, message_codec=LangChainMessageCodec())
    instance.storage.initialize(SESSION_ID, validate_jsonl_items=False)
    return instance


@pytest.fixture
def activation_store(
    sessions_root: Path, storage: RolloutContextStore
) -> ResourceActivationStore:
    detail_store = ContextPlanDetailStore(sessions_root)
    return ResourceActivationStore(
        storage, ActivationLineageBodyStore(storage, detail_store)
    )



def test_save_and_read_round_trip_preserves_identity(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    snapshot = _execution_snapshot(storage=storage)
    body_ref = activation_store.save_snapshot(snapshot)

    assert body_ref.session_id == SESSION_ID
    restored = activation_store.read_snapshot(
        SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID), activation_snapshot_id=snapshot.activation_snapshot_id
    )

    assert restored.to_dict() == snapshot.to_dict()
    assert restored.bindings_hash == snapshot.bindings_hash
    assert restored.activation_provenance_hash == snapshot.activation_provenance_hash

    # 幂等：相同 identity 再次保存返回同一 lineage ref，不新增行。
    assert activation_store.save_snapshot(snapshot) == body_ref
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM resource_activation_snapshots"
        ).fetchone()[0]
    assert count == 1


def test_model_call_snapshot_reuses_parent_bytes(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    turn = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(turn)
    model_call = _model_call_snapshot(turn, storage=storage)
    activation_store.save_snapshot(model_call)

    restored = activation_store.read_snapshot(
        SESSION_ID,
        thread_id=storage.storage.main_thread_id(SESSION_ID),
        activation_snapshot_id=model_call.activation_snapshot_id,
    )

    assert restored.parent is not None
    assert restored.parent.to_dict() == turn.to_dict()
    assert restored.bindings[: len(turn.bindings)] == turn.bindings
    assert restored.parent_execution_snapshot_id == turn.activation_snapshot_id


def test_runtime_without_activation_schema_fails_closed(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    # 模拟已有 rollout 的 activation marker/对象被删除，正常访问必须拒绝。
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        for table in reversed(RESOURCE_ACTIVATION_TABLES):
            connection.execute(f"DROP TABLE {table}")
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.save_snapshot(_execution_snapshot(storage=storage))
    assert error.value.code == SCHEMA_UNAVAILABLE_CODE

    with pytest.raises(ResourceActivationStoreError) as read_error:
        activation_store.read_snapshot(
            SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID), activation_snapshot_id="turn:missing"
        )
    assert read_error.value.code == SCHEMA_UNAVAILABLE_CODE

    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "resource_activation_snapshots" not in tables


def test_unregistered_schema_version_fails_closed(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_schema_state SET activation_schema_version = 99"
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID), activation_snapshot_id="turn:any"
        )
    assert error.value.code == SCHEMA_UNAVAILABLE_CODE


def test_tampered_binding_digest_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    snapshot = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_bindings SET content_hash = ?",
            ("sha256:jcs:v1:" + "b" * 64,),
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id=storage.storage.main_thread_id(SESSION_ID),
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_tampered_lineage_digest_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    """binding 列的 lineage digest 必须与受保护 manifest 逐字节一致。"""

    snapshot = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_bindings SET source_lineage_digest = ?",
            ("sha256:jcs:v1:" + "c" * 64,),
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id=storage.storage.main_thread_id(SESSION_ID),
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_conflicting_snapshot_identity_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    """同一 activation_snapshot_id 不得被不同 binding 选择覆盖。"""

    snapshot = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(snapshot)
    conflicting = ResourceActivationSnapshotRef(
        activation_snapshot_id=snapshot.activation_snapshot_id,
        snapshot_kind="execution",
        activation_policy_revision=snapshot.activation_policy_revision,
        activation_policy_hash=snapshot.activation_policy_hash,
        activation_policy=snapshot.activation_policy,
        registry_generation=snapshot.registry_generation,
        owner_session_id=SESSION_ID,
        owner_thread_id=storage.storage.main_thread_id(SESSION_ID),
        execution_id=snapshot.execution_id,
        turn_id=snapshot.turn_id,
        captured_at=snapshot.captured_at,
        bindings=(
            _binding(
                "skill:demo", ordinal=0, boundary="turn", revision="rev-2"
            , storage=storage),
        ),
    )
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.save_snapshot(conflicting)
    assert error.value.code == "resource-activation-snapshot-conflict"


def test_tampered_catalog_lineage_digest_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    """catalog 的 lineage manifest digest 必须与受保护正文重算一致。"""

    snapshot = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_snapshots SET lineage_manifest_digest = ?",
            ("sha256:jcs:v1:" + "e" * 64,),
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id=storage.storage.main_thread_id(SESSION_ID),
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_tampered_binding_count_is_rejected(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    """snapshot 的 binding_count 必须与实际 binding 行数一致。"""

    snapshot = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(snapshot)
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_snapshots SET binding_count = 7"
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.read_snapshot(
            SESSION_ID,
            thread_id=storage.storage.main_thread_id(SESSION_ID),
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert error.value.code == "resource-activation-hash-mismatch"


def test_lineage_manifest_is_protected_and_absent_from_catalog(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    snapshot = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(snapshot)

    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
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
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        stored_digest = connection.execute(
            "SELECT lineage_manifest_digest FROM resource_activation_snapshots"
        ).fetchone()[0]
    assert stored_digest == lineage_manifest_digest(snapshot)


def test_bind_assembly_requires_committed_snapshot(
    activation_store: ResourceActivationStore, storage: RolloutContextStore
) -> None:
    snapshot = _execution_snapshot(storage=storage)
    with (
        pytest.raises(ResourceActivationStoreError) as error,
        storage.storage._lock(storage.storage.main_thread_id(SESSION_ID), "", session_id=SESSION_ID),
        storage.storage._connect(storage.storage.main_thread_id(SESSION_ID), "", session_id=SESSION_ID) as connection,
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
    with storage.storage._lock(storage.storage.main_thread_id(SESSION_ID), "", session_id=SESSION_ID), storage.storage._connect(storage.storage.main_thread_id(SESSION_ID), "", session_id=SESSION_ID) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO context_assemblies(assembly_id, session_id, thread_id, turn_id, "
            "execution_id, plan_id, plan_hash, request_hash, history_view_revision, "
            "source_overlay_epoch, snapshot_json, model_call_id, status, created_at) "
            "VALUES ('assembly-1', ?, ?, ?, ?, 'plan-1', 'plan-hash', 'request-hash', "
            "0, 0, '{}', NULL, 'sealed', '2026-10-07T00:00:00+00:00')",
            (snapshot.owner_session_id, snapshot.owner_thread_id, snapshot.turn_id, snapshot.execution_id),
        )
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
        SESSION_ID, thread_id=activation_store._storage.storage.main_thread_id(SESSION_ID),
                    assembly_id="assembly-1"
    )
    assert binding["activation_snapshot_id"] == snapshot.activation_snapshot_id
    assert binding["snapshot"].to_dict() == snapshot.to_dict()


def test_fresh_schema_has_current_marker_without_legacy_migration_or_fabricated_rows(
    activation_store: ResourceActivationStore, storage: RolloutContextStore,
) -> None:
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        activation_store.require_schema(connection)
        version = connection.execute(
            "SELECT activation_schema_version FROM resource_activation_schema_state"
        ).fetchone()[0]
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        count = connection.execute(
            "SELECT COUNT(*) FROM resource_activation_snapshots"
        ).fetchone()[0]
    assert version == RESOURCE_ACTIVATION_SCHEMA_VERSION
    assert set(RESOURCE_ACTIVATION_TABLES) <= tables
    assert "resource_activation_schema_migrations" not in tables
    assert "resource_activation_migration_losses" not in tables
    assert count == 0


@pytest.mark.parametrize("marker", [None, 1, 2, 99])
def test_existing_missing_or_old_marker_is_never_repaired(
    activation_store: ResourceActivationStore, storage: RolloutContextStore, marker: int | None,
) -> None:
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        if marker is None:
            connection.execute("DROP TABLE resource_activation_schema_state")
        else:
            connection.execute(
                "UPDATE resource_activation_schema_state SET activation_schema_version = ?", (marker,)
            )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.find_snapshot(
            SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID), activation_snapshot_id="absent"
        )
    assert error.value.code == SCHEMA_UNAVAILABLE_CODE
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'resource_activation_schema_state'"
        ).fetchone()
        if marker is None:
            assert present is None
        else:
            assert connection.execute(
                "SELECT activation_schema_version FROM resource_activation_schema_state"
            ).fetchone() == (marker,)


def test_lineage_detail_classification_guards_exact_body_read(
    sessions_root: Path, storage: RolloutContextStore, activation_store: ResourceActivationStore,
) -> None:
    snapshot = _execution_snapshot(storage=storage)
    body_ref = activation_store.save_snapshot(snapshot)
    detail_store = ContextPlanDetailStore(sessions_root)
    record = detail_record_from_mapping(storage.assemblies.get_context_plan_detail(
        SESSION_ID, snapshot.owner_thread_id, detail_ref=body_ref, checkpoint_ns="",
    ))
    assert (record.detail_kind, record.retention_class) == (
        "resource_activation_lineage", "resource_activation_provenance",
    )
    assert (record.required, record.sensitive, record.protection, record.visibility) == (
        True, True, "protected", "internal",
    )
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        before = tuple(connection.iterdump())
    with pytest.raises(PermissionError, match="未授权读取"):
        detail_store.read(
            session_id=SESSION_ID, thread_id=snapshot.owner_thread_id, record=record,
        )
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        assert tuple(connection.iterdump()) == before
    assert detail_store.read(
        session_id=SESSION_ID, thread_id=snapshot.owner_thread_id, record=record,
        include_sensitive=True,
    )["detail"] == lineage_manifest(snapshot)
    assert activation_store.read_snapshot(
        SESSION_ID, thread_id=snapshot.owner_thread_id,
        activation_snapshot_id=snapshot.activation_snapshot_id,
    ).to_dict() == snapshot.to_dict()


def test_snapshot_cannot_borrow_other_execution_turn(
    activation_store: ResourceActivationStore, storage: RolloutContextStore,
) -> None:
    from dataclasses import replace

    root = _execution_snapshot(storage)
    wrong = replace(root, turn_id="other-turn")
    with pytest.raises(ResourceActivationStoreError) as error:
        activation_store.save_snapshot(wrong)
    assert error.value.code == "resource-activation-snapshot-conflict"
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_activation_snapshots").fetchone() == (0,)


def test_complete_members_live_only_in_protected_manifest_and_survive_restart(
    activation_store: ResourceActivationStore, storage: RolloutContextStore, sessions_root: Path,
) -> None:
    from dataclasses import replace

    first = _execution_snapshot(storage)
    members = (
        ResourceMemberBinding(
            member_id="custom:demo", member_kind="custom", schema_hash="sha256:" + "a" * 64,
            server_id=None, revision="catalog-revision-1",
        ),
        ResourceMemberBinding(
            member_id="mcp:server:demo", member_kind="mcp", schema_hash="sha256:" + "b" * 64,
            server_id="server-demo", revision="catalog-revision-1",
        ),
    )
    snapshot = replace(first, bindings=(replace(first.bindings[0], members=members),))
    body_ref = activation_store.save_snapshot(snapshot)
    assert lineage_manifest(snapshot)["lineages"][0]["members"] == [
        member.to_dict() for member in members
    ]
    assert snapshot.bindings_hash == first.bindings_hash
    assert snapshot.activation_provenance_hash == first.activation_provenance_hash
    assert lineage_manifest_digest(snapshot) != lineage_manifest_digest(first)
    assert "activation_snapshot_id" not in lineage_manifest(snapshot)
    assert lineage_manifest_digest(replace(snapshot, activation_snapshot_id="other-operation")) == lineage_manifest_digest(snapshot)
    protected = activation_store._lineage_body.read_lineage_manifest(
        detail_ref=body_ref, checkpoint_ns="",
        storage_owner=(SESSION_ID, storage.storage.main_thread_id(SESSION_ID)),
    )
    assert protected == lineage_manifest(snapshot)

    restarted = ResourceActivationStore(
        storage,
        ActivationLineageBodyStore(storage, ContextPlanDetailStore(sessions_root)),
    )
    restored = restarted.read_snapshot(
        SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID),
        activation_snapshot_id=snapshot.activation_snapshot_id,
    )
    assert restored.to_dict() == snapshot.to_dict()
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(resource_activation_bindings)")}
    assert "members" not in columns and "targets" not in columns

    for changed in (
        replace(members[0], member_id="custom:other"),
        replace(members[0], schema_hash="sha256:" + "c" * 64),
        replace(members[0], revision="catalog-revision-2"),
        replace(members[1], server_id="server-other"),
        replace(members[1], member_kind="custom", server_id=None),
    ):
        altered = replace(snapshot, bindings=(replace(snapshot.bindings[0], members=(changed,)),))
        assert altered.bindings_hash == snapshot.bindings_hash
        assert altered.activation_provenance_hash == snapshot.activation_provenance_hash
        with pytest.raises(ResourceActivationStoreError) as conflict:
            activation_store.save_snapshot(altered)
        assert conflict.value.code == "resource-activation-hash-mismatch"
        assert restarted.read_snapshot(
            SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID),
            activation_snapshot_id=snapshot.activation_snapshot_id,
        ).to_dict() == snapshot.to_dict()


def test_old_protected_manifest_is_rejected_even_with_valid_catalog_digest(
    activation_store: ResourceActivationStore, storage: RolloutContextStore,
) -> None:
    from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs

    snapshot = _execution_snapshot(storage)
    activation_store.save_snapshot(snapshot)
    old_body = {
        **lineage_manifest(snapshot),
        "schema": "resource-activation-lineage-manifest:v1",
        "activation_snapshot_id": snapshot.activation_snapshot_id,
    }
    old_ref = activation_store._lineage_body.write_lineage_manifest(
        owner_session_id=SESSION_ID, owner_thread_id=storage.storage.main_thread_id(SESSION_ID),
        activation_snapshot_id=snapshot.activation_snapshot_id,
        checkpoint_ns="", manifest=old_body,
    )
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        connection.execute(
            "UPDATE resource_activation_snapshots SET lineage_detail_ref = ?, lineage_manifest_digest = ?",
            (canonical_json_bytes(old_ref.to_dict()).decode(), sha256_jcs(old_body)),
        )
        connection.commit()
    with pytest.raises(ResourceActivationStoreError) as rejected:
        activation_store.read_snapshot(
            SESSION_ID, thread_id=storage.storage.main_thread_id(SESSION_ID),
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
    assert rejected.value.code == "resource-activation-lineage-invalid"
    with closing(sqlite3.connect(storage.storage.index_path(SESSION_ID, ""))) as connection:
        assert connection.execute(
            "SELECT activation_schema_version FROM resource_activation_schema_state"
        ).fetchone() == (RESOURCE_ACTIVATION_SCHEMA_VERSION,)
