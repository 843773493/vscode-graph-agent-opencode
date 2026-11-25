"""9.5：sealed activation ref 的 restore/retention/rewind/fork 保留合同测试。

覆盖四条独立断言：

- retention GC 不回收 sealed activation lineage/resource 正文；
- rewind/compaction 的 control payload 精确保留 source activation ref；
- restore 只读 sealed activation，缺失/schema 未接入时 fail closed，绝不解析
  当前 URI 或 Registry；
- full_rollout_copy 原样保留 activation identity、原 scope 与 source lineage。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.agents.tool_invocation_context import ThreadRuntimeBinding
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id, create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.compaction import (
    ApplyCompactionCandidateIntent,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import InternalExecutionIntent
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
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.activation.store import (
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.support.model_stream.compaction_model import (
    compaction_summary_model as compaction_summary_model,  # noqa: PLC0414 - 本地固定 Provider cassette
)
from tests.support.rollout.compaction_application import generate_compaction_candidate

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
    storage = RolloutContextStore(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    return RolloutCheckpointSaver(
        sessions_dir, storage=storage
    )


def _main_thread_id(saver: RolloutCheckpointSaver, session_id: str = SESSION_ID) -> str:
    return saver._storage.storage.main_thread_id(session_id)


def _activation_snapshot(
    saver: RolloutCheckpointSaver,
    *,
    binding: ThreadRuntimeBinding,
    turn_id: str | None,
    execution_id: str,
) -> ResourceActivationSnapshotRef:
    """写入逐资源正文 detail 并返回引用它的 turn snapshot。"""
    snapshot_id = f"execution:{binding.session_id}:{binding.thread_id}:{execution_id}"
    body_ref = ResourceActivationBodyStore(saver._storage, saver._detail_store).write_resource_bodies(
        owner_session_id=binding.session_id,
        owner_thread_id=binding.thread_id,
        activation_snapshot_id=snapshot_id,
        payloads=({'payload': 'resource-body'},),
        checkpoint_ns='',
    )[0][0]
    body_record = saver._storage.assemblies.get_context_plan_detail(
        binding.session_id, binding.thread_id, detail_ref=body_ref,
    )
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
                content_length=body_record.length,
                content_hash=body_record.source_revision,
                source_lineage_ref=lineage,
                source_lineage_digest=lineage.digest,
                activation_ordinal=0,
                effective_boundary="turn",
                captured_registry_generation=3,
                detail_ref=body_ref,
                members=(ResourceMemberBinding(
                    member_id="custom:demo", member_kind="custom",
                    schema_hash="sha256:" + "a" * 64, server_id=None,
                    revision="catalog-revision-1",
                ),),
            ),
        ),
    )


def _write_admitted_checkpoint(
    saver: RolloutCheckpointSaver, *, internal: bool = False,
) -> tuple[ThreadRuntimeBinding, str | None, str]:
    """通过正式 admission 与 LangGraph owner 写入用户 root 或内部回复。"""
    binding = ThreadRuntimeBinding(
        session_id=SESSION_ID,
        thread_id=saver._storage.storage.main_thread_id(SESSION_ID),
    )
    payload = (
        "请检查本地工作区里的配置、会话历史、工具清单和持久化来源，并说明当前运行状态。"
        "先确认每个来源的真实归属与已保存版本，区分当前会话和历史分支，保留原文引用及完整工具配对。"
        "恢复后继续核对已封存请求和资源正文，不读取当前文件来猜测历史输入，不能把未完成的检查写成成功。"
        "最后列出已经确认的事实、仍然缺失的证据和下一次应执行的验证，保持每个结论能够回到其原始记录。"
    )
    execution_id = f"execution-{create_uuid_hex()}"
    job_id = create_prefixed_id("job")
    user_message_id = f"user-{create_uuid_hex()}"
    if internal:
        saver.accept_turn(
            binding.session_id,
            thread_id=binding.thread_id,
            accepted_ingress_id=create_prefixed_id("evt"),
            acceptance_idempotency_key=f"activation-test:{create_uuid_hex()}:seed-accept",
            job_id=create_prefixed_id("job"),
            admission_idempotency_key=f"activation-test:{create_uuid_hex()}:seed-admission",
            payload=payload,
            turn_id=None,
            root_item_id=f"item-{user_message_id}",
            initial_execution_id=execution_id,
        )
        user_admission = saver.execution_admission(
            binding.session_id,
            thread_id=binding.thread_id,
            execution_id=execution_id,
        )
        owner = MutationIntentOwner(binding.session_id, binding.thread_id)
        with (
            session_thread_owner_scope(owner, langgraph_thread_id=binding.thread_id),
            execution_admission_scope(user_admission, langgraph_thread_id=binding.thread_id),
        ):
            root_config = saver.put(
                build_checkpoint_config(binding.thread_id),
                _checkpoint(
                    "cp-1",
                    [HumanMessage(content=payload, id=user_message_id)],
                ),
                {"source": "resource-activation-test", "step": 1},
                {"messages": "1"},
            )
        execution_id = f"execution-{create_uuid_hex()}"
        job_id = create_prefixed_id("job")
        message_id = f"notice-{create_uuid_hex()}"
        notice = internal_message_factory.build(
            kind="generated_session_result",
            control="资源检查已完成",
            sections=(
                PromptSection("control_context", {"source": "resource-activation-test"}),
                PromptSection("generated_session_result", "已确认冻结资源的原始归属。"),
            ),
            display_content="资源检查已完成。",
        )
        admission = saver.admit_internal_execution(
            binding.session_id, thread_id=binding.thread_id,
            job_id=job_id,
            intent=InternalExecutionIntent(
                message_id=message_id, execution_id=execution_id,
                admission_idempotency_key=f"activation-test:{create_uuid_hex()}:internal",
            ),
            message=HumanMessage(
                content=notice.content, id=message_id,
                response_metadata={
                    "message_id": message_id,
                    "created_at": "2026-10-04T12:00:00+00:00",
                    "message_metadata": notice.metadata,
                },
            ),
        )
        # rewind 的目标必须实际包含该内部执行的成员，不能退回尚未创建它的用户 checkpoint。
        checkpoint = _checkpoint("cp-internal", [
            HumanMessage(content=payload, id=user_message_id),
            AIMessage(content="已核对资源归属，继续使用冻结原件。", id=f"reply-{execution_id}"),
        ])
        checkpoint["channel_versions"] = {"messages": "2"}
        with (
            session_thread_owner_scope(owner, langgraph_thread_id=binding.thread_id),
            execution_admission_scope(admission, langgraph_thread_id=binding.thread_id),
        ):
            saver.put(root_config, checkpoint, {"source": "resource-activation-test", "step": 2}, {"messages": "2"})
        return binding, admission.turn_id, admission.execution_id
    else:
        saver.accept_turn(
            binding.session_id,
            thread_id=binding.thread_id,
            accepted_ingress_id=create_prefixed_id("evt"),
            acceptance_idempotency_key=f"activation-test:{create_uuid_hex()}:accept",
            job_id=job_id,
            admission_idempotency_key=f"activation-test:{create_uuid_hex()}:admission",
            payload=payload,
            turn_id=None,
            root_item_id=f"item-{user_message_id}",
            initial_execution_id=execution_id,
        )
        admission = saver.execution_admission(
            binding.session_id,
            thread_id=binding.thread_id,
            execution_id=execution_id,
        )
        owner = MutationIntentOwner(binding.session_id, binding.thread_id)
        with (
            session_thread_owner_scope(owner, langgraph_thread_id=binding.thread_id),
            execution_admission_scope(admission, langgraph_thread_id=binding.thread_id),
        ):
            saver.put(
                build_checkpoint_config(binding.thread_id),
                _checkpoint(
                    "cp-1",
                    [HumanMessage(content=payload, id=user_message_id)],
                ),
                {"source": "resource-activation-test"},
                {"messages": "1"},
            )
    return binding, admission.turn_id, admission.execution_id


async def _sealed_with_activation(sessions_dir: Path, wire_contract, *, internal: bool = False):
    """保留测试资源正文，经正式 Provider prepare 建立主请求与冻结绑定。"""
    saver = _saver(sessions_dir)
    binding, turn_id, execution_id = _write_admitted_checkpoint(saver, internal=internal)
    saver._storage.storage.initialize(
        binding.session_id,
        thread_id=binding.thread_id,
        validate_jsonl_items=False,
    )
    parent = _activation_snapshot(
        saver,
        binding=binding,
        turn_id=turn_id,
        execution_id=execution_id,
    )
    model_call_id = str(UUID(hex=create_uuid_hex()))
    snapshot = replace(
        parent, activation_snapshot_id=f"model-{model_call_id}", snapshot_kind="model_call",
        parent=parent, model_call_id=model_call_id,
    )
    await saver.save_resource_activation_snapshot(parent)
    await saver.save_resource_activation_snapshot(snapshot)
    tool_set = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    owner = MutationIntentOwner(binding.session_id, binding.thread_id)
    admission = saver.execution_admission(
        binding.session_id, thread_id=binding.thread_id, execution_id=execution_id,
    )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=binding.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=binding.thread_id),
    ):
        prepared = saver.prepare_context_for_provider(
            binding.session_id, binding.thread_id, turn_id=turn_id, execution_id=execution_id,
            plan_creation_idempotency_key="activation-retention-create",
            seal_idempotency_key="activation-retention-seal",
            context_source_manager=ContextSourceManager(
                owner=ContextSourceOwnerKey(binding.session_id, binding.thread_id), control_state_port=saver,
            ),
            tool_set_supplier=lambda: tool_set,
            model_call_id=model_call_id, activation_snapshot=snapshot,
            **wire_contract("activation-retention-test", "chat_completions"),
        )
        sealed = prepared["snapshot"]
        dispatch = saver.consume_prepared_context_for_dispatch(
            binding.session_id, binding.thread_id, execution_id=execution_id, model_call_id=model_call_id,
        )
        assert dispatch is not None and dispatch["assembly_id"] == sealed.assembly_id
        saver.register_model_call(
            binding.session_id, thread_id=binding.thread_id, execution_id=execution_id,
            model_call_id=model_call_id, attempt=1, provider="activation-retention-test",
            assembly_id=sealed.assembly_id,
        )
    return saver, snapshot, sealed


@pytest.mark.asyncio
async def test_retention_protects_sealed_activation_lineage(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
    frozen_provider_wire,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, _sealed = await _sealed_with_activation(sessions_dir, frozen_provider_wire)

    thread_id = _main_thread_id(saver)
    with closing(
        sqlite3.connect(saver._storage.storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots WHERE activation_snapshot_id=?",
            (snapshot.activation_snapshot_id,),
        ).fetchone()[0]
        assert snapshot.parent is not None
        parent_lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots WHERE activation_snapshot_id=?",
            (snapshot.parent.activation_snapshot_id,),
        ).fetchone()[0]
        body_key = connection.execute(
            "SELECT detail_ref FROM resource_activation_bindings WHERE activation_snapshot_id=?",
            (snapshot.activation_snapshot_id,),
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
        sqlite3.connect(saver._storage.storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        # lineage detail 的 assembly 段是 activation snapshot id，无法被
        # context_assemblies 连接命中；retention 必须独立保护它。
        connection.execute(
            "UPDATE context_plan_details SET expires_at = ? WHERE detail_ref IN (?, ?, ?)",
            (past, lineage_key, body_key, parent_lineage_key),
        )
        connection.commit()

    # GC 候选包含模型、执行父链和共享正文，封存引用必须保护完整恢复链。
    assemblies = saver._storage.assemblies
    referenced_details = (
        DetailRef.from_dict(json.loads(lineage_key)),
        DetailRef.from_dict(json.loads(body_key)),
        DetailRef.from_dict(json.loads(parent_lineage_key)),
    )
    expired_details = assemblies.list_expired_context_plan_details(
        SESSION_ID,
        thread_id,
        expired_before=datetime.now(UTC),
    )
    assert set(referenced_details) <= set(expired_details)
    read_connection = assemblies._storage.read_connection
    opened_connections = 0
    traced_queries: list[str] = []

    @contextmanager
    def count_read_connection(
        owner_session_id: str,
        owner_thread_id: str,
        checkpoint_ns: str = "",
    ):
        nonlocal opened_connections
        opened_connections += 1
        with read_connection(owner_session_id, owner_thread_id, checkpoint_ns) as connection:
            connection.set_trace_callback(traced_queries.append)
            yield connection

    monkeypatch.setattr(assemblies._storage, "read_connection", count_read_connection)
    unused_details = tuple(
        DetailRef(
            SESSION_ID,
            thread_id,
            f"unused-assembly-{index}",
            f"unused-detail-{index}",
        )
        for index in range(205)
    )
    candidates = referenced_details + unused_details
    assert assemblies.find_referenced_context_details(
        SESSION_ID,
        thread_id,
        detail_refs=candidates,
    ) == frozenset(referenced_details)
    assert opened_connections == 1
    reference_queries = [
        query for query in traced_queries
        if (
            "FROM context_plan_details" in query
            or "FROM resource_activation_snapshots" in query
            or "FROM resource_activation_bindings" in query
        )
    ]
    assert reference_queries
    assert len(reference_queries) * 20 <= len(candidates)
    assert assemblies.context_assembly_references_detail(
        SESSION_ID,
        thread_id,
        assembly_id=snapshot.activation_snapshot_id,
        detail_ref=referenced_details[0],
    )
    with pytest.raises(RuntimeError, match="detail-retention-protected"):
        saver._storage.assemblies.mark_context_plan_details_unavailable(
            SESSION_ID,
            thread_id,
            detail_refs=referenced_details,
        )
    with closing(
        sqlite3.connect(saver._storage.storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        statuses = {
            row[0]
            for row in connection.execute(
                "SELECT status FROM context_plan_details WHERE detail_ref IN (?, ?, ?)",
                (lineage_key, body_key, parent_lineage_key),
            )
        }
    assert statuses == {"available"}


@pytest.mark.asyncio
async def test_missing_lineage_body_returns_explicit_loss(tmp_path: Path, session_bundle_factory, frozen_provider_wire) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, sealed = await _sealed_with_activation(sessions_dir, frozen_provider_wire)

    thread_id = _main_thread_id(saver)
    with closing(
        sqlite3.connect(saver._storage.storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots WHERE activation_snapshot_id=?",
            (snapshot.activation_snapshot_id,),
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
    binding, turn_id, execution_id = _write_admitted_checkpoint(saver)
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


async def _prepare_compaction_candidate(
    saver: RolloutCheckpointSaver, execution_id: str, model,
):
    thread_id = _main_thread_id(saver)
    admission = saver.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=execution_id)
    owner = MutationIntentOwner(SESSION_ID, thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        source = saver.get_tuple(build_checkpoint_config(thread_id))
    assert source is not None
    frozen, result = await generate_compaction_candidate(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        admission=admission,
        model=model,
        candidate_id=f"activation-boundary-{execution_id}",
        task_id=f"activation-boundary-task-{execution_id}",
    )
    return source.config, frozen, result


def _apply_checkpoint_boundary(
    saver: RolloutCheckpointSaver,
    execution_id: str,
    boundary: str,
    *,
    checkpoint_config=None,
    candidate=None,
) -> None:
    """经 rewind 或真实候选采用建立 checkpoint 边界。"""
    thread_id = _main_thread_id(saver)
    owner = MutationIntentOwner(SESSION_ID, thread_id)
    admission = saver.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=execution_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        config = build_checkpoint_config(thread_id)
        source = saver.get_tuple(config)
        assert source is not None
        if boundary == "rewind":
            saver.rewind(source.config, checkpoint_id=source.config["configurable"]["checkpoint_id"])
        elif boundary == "compaction" and checkpoint_config is not None and candidate is not None:
            with saver._context_reader.open_snapshot(SESSION_ID, thread_id, "") as snapshot:
                expected_view = saver._storage.checkpoints.active_view_id(snapshot)
            if expected_view is None:
                raise RuntimeError("activation-test-active-view-missing: 采用前活动 view 不存在")
            saver.adopt_candidate(
                checkpoint_config,
                ApplyCompactionCandidateIntent(candidate, expected_view),
            )
        else:
            raise ValueError(f"不支持的资源 activation 边界: {boundary}")


@pytest.mark.parametrize("internal", (False, True), ids=("user-turn", "internal-null-turn"))
@pytest.mark.parametrize("boundary", ("rewind", "compaction"))
@pytest.mark.asyncio
async def test_checkpoint_boundary_keeps_activation_ref_after_cold_restore(
    tmp_path: Path, session_bundle_factory, internal: bool, boundary: str, compaction_summary_model, frozen_provider_wire,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, sealed = await _sealed_with_activation(sessions_dir, frozen_provider_wire, internal=internal)
    thread_id = _main_thread_id(saver)
    original_assembly = saver.get_context_assembly(SESSION_ID, thread_id, assembly_id=sealed.assembly_id)
    jsonl = saver._storage.storage.jsonl_path(SESSION_ID, "", thread_id=thread_id)
    original_bytes = jsonl.read_bytes()
    compaction_result = None

    if boundary == "compaction":
        checkpoint_config, candidate, compaction_result = await _prepare_compaction_candidate(
            saver, snapshot.execution_id, compaction_summary_model,
        )
        _apply_checkpoint_boundary(
            saver, snapshot.execution_id, boundary,
            checkpoint_config=checkpoint_config, candidate=candidate,
        )
    else:
        _apply_checkpoint_boundary(saver, snapshot.execution_id, boundary)
    cold = _saver(sessions_dir)
    with closing(
        sqlite3.connect(cold._storage.storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM control_events WHERE control_kind = ? "
                "ORDER BY control_sequence DESC LIMIT 1",
                ("rewind" if boundary == "rewind" else "checkpoint_created",),
            ).fetchone()[0]
        )
    primary_ref = {
        "assembly_id": sealed.assembly_id,
        "turn_id": snapshot.turn_id,
        "activation_snapshot_id": snapshot.activation_snapshot_id,
        "plan_hash": sealed.plan_hash,
        "request_hash": sealed.request_hash,
        "bindings_hash": snapshot.bindings_hash,
        "activation_provenance_hash": snapshot.activation_provenance_hash,
    }
    expected_refs = [primary_ref]
    expected_snapshots = {sealed.assembly_id: snapshot}
    if compaction_result is not None:
        dispatch_ref = compaction_result.dispatch_ref
        assert dispatch_ref.session_id == SESSION_ID
        assert dispatch_ref.thread_id == thread_id
        derived_assembly = cold.get_context_assembly(
            SESSION_ID, thread_id, assembly_id=dispatch_ref.assembly_id,
        )
        dispatch_ref.validate_snapshot(derived_assembly)
        derived_binding = cold.read_assembly_activation_binding(
            SESSION_ID, thread_id=thread_id, assembly_id=dispatch_ref.assembly_id,
        )
        assert derived_binding is not None
        assert derived_binding["plan_hash"] == dispatch_ref.plan_hash
        assert derived_binding["request_hash"] == dispatch_ref.request_hash
        derived_snapshot = derived_binding["snapshot"]
        assert isinstance(derived_snapshot, ResourceActivationSnapshotRef)
        assert (derived_snapshot.execution_id, derived_snapshot.turn_id) == (
            dispatch_ref.execution_id, dispatch_ref.turn_id,
        )
        assert derived_binding["assembly_id"] == dispatch_ref.assembly_id
        expected_refs.append({
            "assembly_id": dispatch_ref.assembly_id,
            "turn_id": dispatch_ref.turn_id,
            "activation_snapshot_id": derived_snapshot.activation_snapshot_id,
            "plan_hash": dispatch_ref.plan_hash,
            "request_hash": dispatch_ref.request_hash,
            "bindings_hash": derived_snapshot.bindings_hash,
            "activation_provenance_hash": derived_snapshot.activation_provenance_hash,
        })
        expected_snapshots[dispatch_ref.assembly_id] = derived_snapshot
    refs = payload["resource_activation_refs"]
    expected_refs.sort(key=lambda ref: (ref["turn_id"] or "", ref["assembly_id"]))
    assert refs == expected_refs
    for ref in refs:
        restored_ref = cold.load_sealed_resource_activation(
            SESSION_ID, thread_id=thread_id, assembly_id=ref["assembly_id"],
            plan_hash=ref["plan_hash"], request_hash=ref["request_hash"],
        )
        assert restored_ref.to_dict() == expected_snapshots[ref["assembly_id"]].to_dict()
    assert len(expected_refs) == (2 if compaction_result is not None else 1)
    primary_activation = cold.load_sealed_resource_activation(
        SESSION_ID, thread_id=thread_id, assembly_id=sealed.assembly_id,
        plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
    )
    assert ResourceActivationBodyStore(cold._storage, cold._detail_store).read_payload(
        snapshot=primary_activation, binding=primary_activation.bindings[0],
        storage_owner=(SESSION_ID, thread_id),
    ) == {"payload": "resource-body"}
    assert cold.get_context_assembly(SESSION_ID, thread_id, assembly_id=sealed.assembly_id) == original_assembly
    assert jsonl.read_bytes().startswith(original_bytes)
    assert (snapshot.turn_id is None) == internal


@pytest.mark.parametrize("internal", (False, True), ids=("user-turn", "internal-null-turn"))
@pytest.mark.parametrize("boundary", ("rewind", "compaction"))
@pytest.mark.asyncio
async def test_checkpoint_boundary_rejects_activation_owner_mismatch_without_writes(
    tmp_path: Path, session_bundle_factory, internal: bool, boundary: str, compaction_summary_model, frozen_provider_wire,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, _sealed = await _sealed_with_activation(sessions_dir, frozen_provider_wire, internal=internal)
    thread_id = _main_thread_id(saver)
    index = saver._storage.storage.index_path(SESSION_ID, "", thread_id=thread_id)
    jsonl = saver._storage.storage.jsonl_path(SESSION_ID, "", thread_id=thread_id)
    checkpoint_config = candidate = None
    if boundary == "compaction":
        checkpoint_config, candidate, _result = await _prepare_compaction_candidate(
            saver, snapshot.execution_id, compaction_summary_model,
        )
    with closing(sqlite3.connect(index)) as connection:
        connection.execute("UPDATE resource_activation_assembly_bindings SET thread_id='invalid-owner'")
        connection.commit()
        before = tuple(connection.iterdump())
    original_bytes = jsonl.read_bytes()

    with pytest.raises(ValueError, match="source-mismatch"):
        _apply_checkpoint_boundary(
            saver, snapshot.execution_id, boundary,
            checkpoint_config=checkpoint_config, candidate=candidate,
        )

    with closing(sqlite3.connect(index)) as connection:
        assert tuple(connection.iterdump()) == before
    assert jsonl.read_bytes() == original_bytes


@pytest.mark.parametrize("boundary", ("rewind", "compaction"))
@pytest.mark.asyncio
async def test_boundary_payload_uses_only_actual_activation_references(
    tmp_path: Path, session_bundle_factory, boundary: str, compaction_summary_model,
) -> None:
    """source 无 activation 时不造主引用；真实 compaction 请求仍记录自己的冻结引用。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = _saver(sessions_dir)
    binding, _turn_id, execution_id = _write_admitted_checkpoint(saver)
    result = None
    with closing(
        sqlite3.connect(saver._storage.storage.index_path(
            binding.session_id, "", thread_id=binding.thread_id,
        ))
    ) as connection:
        assert connection.execute(
            "SELECT 1 FROM resource_activation_assembly_bindings LIMIT 1"
        ).fetchone() is None
    if boundary == "compaction":
        checkpoint_config, candidate, result = await _prepare_compaction_candidate(
            saver, execution_id, compaction_summary_model,
        )
        _apply_checkpoint_boundary(
            saver, execution_id, boundary,
            checkpoint_config=checkpoint_config, candidate=candidate,
        )
    else:
        _apply_checkpoint_boundary(saver, execution_id, boundary)
    with closing(
        sqlite3.connect(
            saver._storage.storage.index_path(
                binding.session_id, "", thread_id=binding.thread_id
            )
        )
    ) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM control_events WHERE control_kind = ? "
                "ORDER BY control_sequence DESC LIMIT 1",
                ("rewind" if boundary == "rewind" else "checkpoint_created",),
            ).fetchone()[0]
        )
    if boundary == "rewind":
        assert "resource_activation_refs" not in payload
        return

    assert result is not None
    dispatch_ref = result.dispatch_ref
    assert dispatch_ref.session_id == binding.session_id
    assert dispatch_ref.thread_id == binding.thread_id
    derived_assembly = saver.get_context_assembly(
        binding.session_id, binding.thread_id, assembly_id=dispatch_ref.assembly_id,
    )
    dispatch_ref.validate_snapshot(derived_assembly)
    derived_binding = saver.read_assembly_activation_binding(
        binding.session_id, thread_id=binding.thread_id,
        assembly_id=dispatch_ref.assembly_id,
    )
    assert derived_binding is not None
    assert derived_binding["assembly_id"] == dispatch_ref.assembly_id
    assert derived_binding["plan_hash"] == dispatch_ref.plan_hash
    assert derived_binding["request_hash"] == dispatch_ref.request_hash
    derived_snapshot = derived_binding["snapshot"]
    assert isinstance(derived_snapshot, ResourceActivationSnapshotRef)
    assert derived_snapshot.bindings == ()
    assert payload["resource_activation_refs"] == [{
        "assembly_id": dispatch_ref.assembly_id,
        "turn_id": dispatch_ref.turn_id,
        "activation_snapshot_id": derived_snapshot.activation_snapshot_id,
        "plan_hash": dispatch_ref.plan_hash,
        "request_hash": dispatch_ref.request_hash,
        "bindings_hash": derived_snapshot.bindings_hash,
        "activation_provenance_hash": derived_snapshot.activation_provenance_hash,
    }]
    assert saver.load_sealed_resource_activation(
        binding.session_id, thread_id=binding.thread_id,
        assembly_id=dispatch_ref.assembly_id,
        plan_hash=dispatch_ref.plan_hash, request_hash=dispatch_ref.request_hash,
    ).to_dict() == derived_snapshot.to_dict()


@pytest.mark.asyncio
async def test_retention_lineage_detail_manifest_is_registered(tmp_path: Path, session_bundle_factory, frozen_provider_wire) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver, snapshot, _sealed = await _sealed_with_activation(sessions_dir, frozen_provider_wire)
    thread_id = _main_thread_id(saver)
    with closing(
        sqlite3.connect(saver._storage.storage.index_path(SESSION_ID, "", thread_id=thread_id))
    ) as connection:
        lineage_key = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots WHERE activation_snapshot_id=?",
            (snapshot.activation_snapshot_id,),
        ).fetchone()[0]
    record = saver._storage.assemblies.get_context_plan_detail(
        SESSION_ID,
        thread_id,
        detail_ref=DetailRef.from_dict(json.loads(lineage_key)),
    )
    assert record.detail_kind == "resource_activation_lineage"
    assert record.required is True
