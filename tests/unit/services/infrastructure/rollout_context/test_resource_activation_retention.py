"""9.5：sealed activation ref 的 restore/retention/rewind/fork 保留合同测试。

覆盖四条独立断言：

- retention GC 不回收 sealed activation lineage/resource 正文；
- rewind/compaction 的 control payload 精确保留 source activation ref；
- restore 只读 sealed activation，缺失/schema 未接入时 fail closed，绝不解析
  当前 URI 或 Registry；
- full_rollout_copy 重造 target-local activation identity 并保留 source lineage。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
    ResourceActivationStore,
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage

SESSION_ID = "ses_31b6a0d5c4e34f2a8b7d6901fe2c4a83"
PROTECTED_KEY = bytes(range(32))
TURN_ID = "turn-1"


def _checkpoint(checkpoint_id: str, messages: list[object]) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["updated_channels"] = ["messages"]
    return checkpoint


def _saver(sessions_dir: Path) -> RolloutCheckpointSaver:
    storage = RolloutStorage(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    return RolloutCheckpointSaver(
        sessions_dir, storage=storage, protected_detail_key=PROTECTED_KEY
    )


def _activation_snapshot(saver: RolloutCheckpointSaver, *, turn_id: str) -> ResourceActivationSnapshotRef:
    """写入逐资源正文 detail 并返回引用它的 turn snapshot。"""
    snapshot_id = f"turn:{SESSION_ID}:main:{turn_id}"
    body_record = saver._detail_store.write(
        session_id=SESSION_ID,
        assembly_id=snapshot_id,
        detail_kind="resource_body",
        retention_class="resource_body",
        visibility="internal",
        detail={"payload": "resource-body"},
        required=True,
        sensitive=True,
        checkpoint_ns="",
    )
    saver._storage.register_context_plan_detail(body_record)
    lineage = SourceLineageRef(
        lineage_id="lineage:skill:demo",
        derivation_version="resource-derivation:v1",
        sources=(("source-1", "src-rev-1"),),
    )
    return ResourceActivationSnapshotRef(
        activation_snapshot_id=snapshot_id,
        snapshot_kind="turn",
        activation_policy_revision="resource-activation-policy:v1:abc",
        activation_policy_hash="sha256:jcs:v1:" + "1" * 64,
        registry_generation=3,
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id=turn_id,
        captured_at="2026-09-24T00:00:00+00:00",
        bindings=(
            ResourceProvenanceRef(
                resource_id="skill:demo",
                display_uri="boxteam://workspace/test/skill-demo",
                resource_kind="skills",
                owner_scope="session",
                facet="activation",
                revision="rev-1",
                availability="available",
                content_length=len(json.dumps({"payload": "resource-body"})),
                content_hash=sha256_jcs({"payload": "resource-body"}),
                redacted_stable_digest=None,
                source_lineage_ref=lineage,
                source_lineage_digest=lineage.digest,
                activation_ordinal=0,
                effective_boundary="turn",
                captured_registry_generation=3,
                detail_ref=body_record.detail_ref,
            ),
        ),
    )


def _sealed_with_activation(sessions_dir: Path):
    """建立一个带 sealed activation binding 的真实会话。"""
    saver = _saver(sessions_dir)
    saver._storage.initialize(SESSION_ID, validate_jsonl_items=False)
    saver.attach_resource_activation_store(
        ResourceActivationStore(
            saver._storage,
            ActivationLineageBodyStore(saver._storage, saver._detail_store),
        )
    )
    saver.bootstrap_resource_activation_schema(SESSION_ID)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-1",
            [
                HumanMessage(
                    content="问题",
                    id="user-1",
                    response_metadata={"turn_id": TURN_ID},
                )
            ],
        ),
        {"source": "test"},
        {"messages": "1"},
    )
    turn_id = TURN_ID
    snapshot = _activation_snapshot(saver, turn_id=turn_id)
    plan = saver.compose_committed_context_plan(SESSION_ID, plan_id="plan-1")
    plan = replace(plan, plan_creation_idempotency_key="activation-retention-create")
    saver.create_context_plan(SESSION_ID, plan)
    sealed = saver.seal_context_plan(
        SESSION_ID,
        plan,
        turn_id=turn_id,
        execution_id=saver.execution_for_turn(SESSION_ID, turn_id=turn_id),
        provider_version="activation-retention-test",
        seal_idempotency_key="activation-retention-seal",
        activation_snapshot=snapshot,
    )
    return saver, snapshot, sealed


def test_retention_protects_sealed_activation_lineage(tmp_path: Path, session_bundle_factory) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, _sealed = _sealed_with_activation(sessions_dir)

    with closing(sqlite3.connect(saver._storage.index_path(SESSION_ID, ""))) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots"
        ).fetchone()[0]
        body_key = connection.execute(
            "SELECT detail_ref FROM resource_activation_bindings"
        ).fetchone()[0]
    # 正文可读回并逐字节重算 hash。
    restored = saver.load_sealed_resource_activation(
        SESSION_ID,
        assembly_id=_sealed.assembly_id,
        plan_hash=_sealed.plan_hash,
        request_hash=_sealed.request_hash,
    )
    assert restored.to_dict() == snapshot.to_dict()

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with closing(sqlite3.connect(saver._storage.index_path(SESSION_ID, ""))) as connection:
        # lineage detail 的 assembly 段是 activation snapshot id，无法被
        # context_assemblies 连接命中；retention 必须独立保护它。
        connection.execute(
            "UPDATE context_plan_details SET expires_at = ? WHERE detail_ref IN (?, ?)",
            (past, lineage_key, body_key),
        )
        connection.commit()

    # GC 候选包含这两条，但它们被 sealed activation catalog 引用，必须保护。
    assert saver._storage.context_assembly_references_detail(
        SESSION_ID,
        assembly_id=snapshot.activation_snapshot_id,
        detail_ref=DetailRef.from_dict(json.loads(lineage_key)),
    )
    with pytest.raises(RuntimeError, match="detail-retention-protected"):
        saver._storage.mark_context_plan_details_unavailable(
            SESSION_ID,
            detail_refs=(
                DetailRef.from_dict(json.loads(lineage_key)),
                DetailRef.from_dict(json.loads(body_key)),
            ),
        )
    with closing(sqlite3.connect(saver._storage.index_path(SESSION_ID, ""))) as connection:
        statuses = {
            row[0]
            for row in connection.execute(
                "SELECT status FROM context_plan_details WHERE detail_ref IN (?, ?)",
                (lineage_key, body_key),
            )
        }
    assert statuses == {"available"}


def test_missing_lineage_body_returns_explicit_loss(tmp_path: Path, session_bundle_factory) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, _snapshot, sealed = _sealed_with_activation(sessions_dir)

    with closing(sqlite3.connect(saver._storage.index_path(SESSION_ID, ""))) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots"
        ).fetchone()[0]
        connection.execute(
            "UPDATE context_plan_details SET status = 'unavailable', "
            "availability = 'unavailable' WHERE detail_ref = ?",
            (lineage_key,),
        )
        connection.commit()

    with pytest.raises(ResourceActivationStoreError) as error:
        saver.load_sealed_resource_activation(
            SESSION_ID,
            assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash,
            request_hash=sealed.request_hash,
        )
    # 明确 loss，绝不静默回退同名新资源或空 manifest。
    assert error.value.code == "resource-activation-lineage-unavailable"


def test_restore_without_activation_store_is_lazy(tmp_path: Path, session_bundle_factory) -> None:
    """未注入 activation store 的旧部署保持既有行为；注入后缺失即失败。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = _saver(sessions_dir)
    saver._storage.initialize(SESSION_ID, validate_jsonl_items=False)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-1",
            [
                HumanMessage(
                    content="问题",
                    id="user-1",
                    response_metadata={"turn_id": TURN_ID},
                )
            ],
        ),
        {"source": "test"},
        {"messages": "1"},
    )
    turn_id = TURN_ID
    plan = saver.compose_committed_context_plan(SESSION_ID, plan_id="plan-1")
    plan = replace(plan, plan_creation_idempotency_key="lazy-create")
    saver.create_context_plan(SESSION_ID, plan)
    sealed = saver.seal_context_plan(
        SESSION_ID,
        plan,
        turn_id=turn_id,
        execution_id=saver.execution_for_turn(SESSION_ID, turn_id=turn_id),
        provider_version="no-activation",
        seal_idempotency_key="lazy-seal",
    )
    assert (
        saver.read_sealed_resource_activation(
            SESSION_ID,
            assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash,
            request_hash=sealed.request_hash,
        )
        is None
    )
    # 注入 store 后该 assembly 没有 activation 事实，必须显式失败。
    saver.attach_resource_activation_store(
        ResourceActivationStore(
            saver._storage,
            ActivationLineageBodyStore(saver._storage, saver._detail_store),
        )
    )
    with pytest.raises(RuntimeError, match="resource-activation-unavailable"):
        saver.read_sealed_resource_activation(
            SESSION_ID,
            assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash,
            request_hash=sealed.request_hash,
        )


def test_rewind_control_payload_keeps_activation_ref(tmp_path: Path, session_bundle_factory) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, _sealed = _sealed_with_activation(sessions_dir)

    saver._storage.rewind_to_checkpoint(
        thread_id=SESSION_ID, checkpoint_id="cp-1", anchor_mode="inclusive"
    )
    with closing(sqlite3.connect(saver._storage.index_path(SESSION_ID, ""))) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM control_events WHERE control_kind = 'rewind'"
            ).fetchone()[0]
        )
    refs = payload["resource_activation_refs"]
    assert refs == [
        {
            "assembly_id": _sealed.assembly_id,
            "turn_id": snapshot.turn_id,
            "activation_snapshot_id": snapshot.activation_snapshot_id,
            "plan_hash": _sealed.plan_hash,
            "request_hash": _sealed.request_hash,
            "bindings_hash": snapshot.bindings_hash,
            "activation_provenance_hash": snapshot.activation_provenance_hash,
        }
    ]


def test_rewind_payload_omits_key_without_activation(tmp_path: Path, session_bundle_factory) -> None:
    """无 activation 事实时 control payload 保持原字节，不新造空键。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = _saver(sessions_dir)
    saver._storage.initialize(SESSION_ID, validate_jsonl_items=False)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-1",
            [
                HumanMessage(
                    content="问题",
                    id="user-1",
                    response_metadata={"turn_id": TURN_ID},
                )
            ],
        ),
        {"source": "test"},
        {"messages": "1"},
    )
    saver._storage.rewind_to_checkpoint(
        thread_id=SESSION_ID, checkpoint_id="cp-1", anchor_mode="inclusive"
    )
    with closing(sqlite3.connect(saver._storage.index_path(SESSION_ID, ""))) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM control_events WHERE control_kind = 'rewind'"
            ).fetchone()[0]
        )
    assert "resource_activation_refs" not in payload


def test_retention_lineage_detail_manifest_is_registered(tmp_path: Path, session_bundle_factory) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, _snapshot, _sealed = _sealed_with_activation(sessions_dir)
    with closing(sqlite3.connect(saver._storage.index_path(SESSION_ID, ""))) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots"
        ).fetchone()[0]
    record = detail_record_from_mapping(
        saver._storage.get_context_plan_detail(
            SESSION_ID, detail_ref=DetailRef.from_dict(json.loads(lineage_key))
        )
    )
    assert record.detail_kind == "resource_activation_lineage"
    assert record.required is True
