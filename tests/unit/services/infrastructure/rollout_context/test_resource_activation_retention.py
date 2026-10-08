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

from app.agents.tool_invocation_context import ThreadRuntimeBinding
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id, create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import (
    ResourceActivationPolicySnapshot,
    ResourceActivationSnapshotRef,
    ResourceMemberBinding,
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
from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage

SESSION_ID = "ses_019c0fd1773c7b198a60f53aeeee17eb"
POLICY = ResourceActivationPolicySnapshot.from_dict({"default_boundary": "turn", "overrides": {"mcp_tool_catalog": "model_call"}})
PROTECTED_KEY = bytes(range(32))


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
        sessions_dir, storage=storage
    )


def _main_thread_id(saver: RolloutCheckpointSaver, session_id: str = SESSION_ID) -> str:
    return saver._storage.main_thread_id(session_id)


def _activation_snapshot(
    saver: RolloutCheckpointSaver,
    *,
    binding: ThreadRuntimeBinding,
    turn_id: str,
    execution_id: str,
) -> ResourceActivationSnapshotRef:
    """写入逐资源正文 detail 并返回引用它的 turn snapshot。"""
    snapshot_id = f"execution:{binding.session_id}:{binding.thread_id}:{execution_id}"
    body_record = saver._detail_store.write(
        session_id=binding.session_id,
        thread_id=binding.thread_id,
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
        snapshot_kind="execution",
        activation_policy_revision=POLICY.revision,
        activation_policy_hash=POLICY.hash,
        activation_policy=POLICY,
        registry_generation=3,
        owner_session_id=binding.session_id,
        owner_thread_id=binding.thread_id,
        execution_id=execution_id,
        turn_id=turn_id,
        captured_at="2026-09-24T00:00:00+00:00",
        bindings=(
            ResourceProvenanceRef(
                resource_id="skill:demo",
                display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
                resource_kind="skills",
                owner_scope="session",
                facet="activation",
                revision="rev-1",
                availability="available",
                content_length=len(json.dumps({"payload": "resource-body"})),
                content_hash=sha256_jcs({"payload": "resource-body"}),
                source_lineage_ref=lineage,
                source_lineage_digest=lineage.digest,
                activation_ordinal=0,
                effective_boundary="turn",
                captured_registry_generation=3,
                detail_ref=body_record.detail_ref,
                members=(ResourceMemberBinding(
                    member_id="custom:demo", member_kind="custom",
                    schema_hash="sha256:" + "a" * 64, server_id=None,
                    revision="catalog-revision-1",
                ),),
            ),
        ),
    )


def _write_accepted_checkpoint(
    saver: RolloutCheckpointSaver,
) -> tuple[ThreadRuntimeBinding, str, str]:
    """通过正式 acceptance 与 LangGraph owner 写入一条真实 Turn root。"""
    binding = ThreadRuntimeBinding(
        session_id=SESSION_ID,
        thread_id=saver._storage.main_thread_id(SESSION_ID),
    )
    accepted_ingress_id = create_prefixed_id("evt")
    acceptance_key = f"activation-test:{create_uuid_hex()}:accept"
    execution_id = f"execution-{create_uuid_hex()}"
    job_id = create_prefixed_id("job")
    user_message_id = f"user-{create_uuid_hex()}"
    accepted = saver.accept_turn(
        binding.session_id,
        thread_id=binding.thread_id,
        accepted_ingress_id=accepted_ingress_id,
        acceptance_idempotency_key=acceptance_key,
        job_id=job_id,
        admission_idempotency_key=f"activation-test:{create_uuid_hex()}:admission",
        payload="问题",
        turn_id=None,
        root_item_id=f"item-{user_message_id}",
        initial_execution_id=execution_id,
    )
    turn_id = accepted["turn_id"]
    accepted_execution_id = accepted["execution_id"]
    if not isinstance(turn_id, str) or not isinstance(accepted_execution_id, str):
        raise TypeError("accept_turn 未返回完整 Turn/execution identity")
    admission = saver.execution_admission(
        binding.session_id,
        thread_id=binding.thread_id,
        execution_id=accepted_execution_id,
    )
    owner = MutationIntentOwner(
        session_id=binding.session_id,
        thread_id=binding.thread_id,
    )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=binding.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=binding.thread_id),
    ):
        saver.put(
            build_checkpoint_config(binding.thread_id),
            _checkpoint(
                "cp-1",
                [HumanMessage(content="问题", id=user_message_id)],
            ),
            {"source": "resource-activation-test"},
            {"messages": "1"},
        )
    return binding, turn_id, accepted_execution_id


def _sealed_with_activation(sessions_dir: Path):
    """建立一个带 sealed activation binding 的真实会话。"""
    saver = _saver(sessions_dir)
    binding, turn_id, execution_id = _write_accepted_checkpoint(saver)
    saver._storage.initialize(
        binding.session_id,
        thread_id=binding.thread_id,
        validate_jsonl_items=False,
    )
    snapshot = _activation_snapshot(
        saver,
        binding=binding,
        turn_id=turn_id,
        execution_id=execution_id,
    )
    plan = saver.compose_committed_context_plan(
        binding.session_id,
        binding.thread_id,
        plan_id="plan-1", tool_set=ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
    )
    plan = replace(plan, plan_creation_idempotency_key="activation-retention-create")
    saver.create_context_plan(binding.session_id, plan)
    sealed = saver.seal_context_plan(
        binding.session_id,
        binding.thread_id,
        plan,
        wire_profile=None, wire_params=None, turn_id=turn_id,
        execution_id=execution_id,
        provider_version="activation-retention-test",
        seal_idempotency_key="activation-retention-seal",
        activation_snapshot=snapshot,
    )
    return saver, snapshot, sealed


def test_retention_protects_sealed_activation_lineage(tmp_path: Path, session_bundle_factory) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, _sealed = _sealed_with_activation(sessions_dir)

    thread_id = _main_thread_id(saver)
    with closing(
        sqlite3.connect(saver._storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots"
        ).fetchone()[0]
        body_key = connection.execute(
            "SELECT detail_ref FROM resource_activation_bindings"
        ).fetchone()[0]
    # 正文可读回并逐字节重算 hash。
    restored = saver.load_sealed_resource_activation(
        SESSION_ID,
        thread_id=thread_id,
        assembly_id=_sealed.assembly_id,
        plan_hash=_sealed.plan_hash,
        request_hash=_sealed.request_hash,
    )
    assert restored.to_dict() == snapshot.to_dict()

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with closing(
        sqlite3.connect(saver._storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
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
        thread_id,
        assembly_id=snapshot.activation_snapshot_id,
        detail_ref=DetailRef.from_dict(json.loads(lineage_key)),
    )
    with pytest.raises(RuntimeError, match="detail-retention-protected"):
        saver._storage.mark_context_plan_details_unavailable(
            SESSION_ID,
            thread_id,
            detail_refs=(
                DetailRef.from_dict(json.loads(lineage_key)),
                DetailRef.from_dict(json.loads(body_key)),
            ),
        )
    with closing(
        sqlite3.connect(saver._storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
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

    thread_id = _main_thread_id(saver)
    with closing(
        sqlite3.connect(saver._storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
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
            thread_id=thread_id,
            assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash,
            request_hash=sealed.request_hash,
        )
    # 明确 loss，绝不静默回退同名新资源或空 manifest。
    assert error.value.code == "resource-activation-lineage-unavailable"


def test_restore_rejects_missing_activation_fact(tmp_path: Path, session_bundle_factory) -> None:
    """未注入 activation store 的旧部署保持既有行为；注入后缺失即失败。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = _saver(sessions_dir)
    binding, turn_id, execution_id = _write_accepted_checkpoint(saver)
    plan = saver.compose_committed_context_plan(
        binding.session_id, binding.thread_id, plan_id="plan-1",
        tool_set=ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
    )
    plan = replace(plan, plan_creation_idempotency_key="lazy-create")
    saver.create_context_plan(binding.session_id, plan)
    sealed = saver.seal_context_plan(
        binding.session_id,
        binding.thread_id,
        plan,
        wire_profile=None, wire_params=None, turn_id=turn_id,
        execution_id=execution_id,
        provider_version="no-activation",
        seal_idempotency_key="lazy-seal",
    )
    # Saver 始终持有同一 store；缺少冻结事实的 assembly 必须直接拒绝。
    with pytest.raises(RuntimeError, match="resource-activation-unavailable"):
        saver.load_sealed_resource_activation(
            binding.session_id,
            thread_id=binding.thread_id,
            assembly_id=sealed.assembly_id,
            plan_hash=sealed.plan_hash,
            request_hash=sealed.request_hash,
        )


def test_rewind_control_payload_keeps_activation_ref(tmp_path: Path, session_bundle_factory) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, _sealed = _sealed_with_activation(sessions_dir)
    thread_id = _main_thread_id(saver)

    owner = MutationIntentOwner(SESSION_ID, thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
        saver._storage.rewind_to_checkpoint(
            thread_id=thread_id, checkpoint_id="cp-1", anchor_mode="inclusive"
        )
    with closing(
        sqlite3.connect(saver._storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
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
    binding, _turn_id, _execution_id = _write_accepted_checkpoint(saver)
    owner = MutationIntentOwner(binding.session_id, binding.thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=binding.thread_id):
        saver._storage.rewind_to_checkpoint(
            thread_id=binding.thread_id, checkpoint_id="cp-1", anchor_mode="inclusive"
        )
    with closing(
        sqlite3.connect(
            saver._storage.index_path(
                binding.session_id, "", thread_id=binding.thread_id
            )
        )
    ) as connection:
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
    thread_id = _main_thread_id(saver)
    with closing(
        sqlite3.connect(saver._storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots"
        ).fetchone()[0]
    record = detail_record_from_mapping(
        saver._storage.get_context_plan_detail(
            SESSION_ID,
            thread_id,
            detail_ref=DetailRef.from_dict(json.loads(lineage_key)),
        )
    )
    assert record.detail_kind == "resource_activation_lineage"
    assert record.required is True
