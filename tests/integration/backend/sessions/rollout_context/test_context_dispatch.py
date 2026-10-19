"""验证真实 Saver dispatch 选择当前 prompt 和持久 overlay 的共同来源。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
)
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
)
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def dispatch_session(
    request: pytest.FixtureRequest,
    session_bundle_factory: Callable[[Path, str], Path],
) -> Iterator[tuple[RolloutCheckpointSaver, str, str, Path]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    saver = RolloutCheckpointSaver(sessions)
    accepted = saver.accept_turn(
        session_id,
        thread_id=saver._storage.storage.main_thread_id(session_id), accepted_ingress_id="dispatch-ingress",
        acceptance_idempotency_key="dispatch-key",
        job_id="dispatch-job",
        admission_idempotency_key="dispatch-admission",
        payload="请基于最新策略继续工作",
        payload_kind=PayloadKind.TEXT,
    )
    thread = saver._storage.storage.main_thread_id(session_id)
    admission = saver.execution_admission(
        session_id, thread_id=thread, execution_id=str(accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread), langgraph_thread_id=thread,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        yield saver, session_id, str(accepted["turn_id"]), sessions


EMPTY_TOOL_SET = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


@pytest.fixture
def dispatch_manager(dispatch_session) -> ContextSourceManager:
    saver, session, _turn, _sessions = dispatch_session
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session, saver._storage.storage.main_thread_id(session)),
        control_state_port=saver,
    )
    manager.register(ContextSourceDescriptor(
        source_id="agents:dispatch", source_kind="workspace_agents",
        name="dispatch policy", description="当前模型调用来源",
        internal_locator="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md",
    ), tracking_status="tracked")
    return manager


def _prompt(identity: str) -> ContextContribution:
    body = {"policy": identity}
    return ContextContribution(
        contribution_id=identity,
        source_kind="workspace_policy",
        source_revision=identity,
        body=body,
        content_hash=contribution_content_hash("prompt", body),
    )


def test_dispatch_projects_pending_source_to_chat_and_responses(frozen_provider_activation,
    frozen_provider_wire, dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    dispatch_manager: ContextSourceManager,
) -> None:
    """sealed assembly 中的 source item 必须进入两种 Provider wire。"""
    saver, session_id, turn_id, _sessions = dispatch_session
    source_body = "# 源码调试工具\n\n通过固定信封调用调试目标。"
    dispatch_manager.register(ContextSourceDescriptor(
        source_id="skill:debugging", source_kind="skill", name="debugging",
        description="源码调试工具", internal_locator="skill-entry:test:debugging",
    ), tracking_status="tracked")
    dispatch_manager.observe("skill:debugging", source_body, revision="debugging-v1")

    prepared = saver.prepare_context_for_provider(session_id, saver._storage.storage.main_thread_id(session_id), turn_id=turn_id, **frozen_provider_wire('contract-provider-v2', 'chat_completions'),  plan_creation_idempotency_key='pending-source-chat:create', seal_idempotency_key='pending-source-chat:seal', execution_id=saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id=turn_id), context_source_manager=dispatch_manager, tool_set_supplier=lambda: EMPTY_TOOL_SET, **frozen_provider_activation(saver, plan_creation_idempotency_key='pending-source-chat:create'))

    assert any(
        message.type == "human" and message.text == source_body
        for message in prepared["messages"]
    )
    native = saver.project_context_plan_to_native(session_id, saver._storage.storage.main_thread_id(session_id), prepared['plan'])
    assert {
        "role": "user",
        "content": [{"type": "input_text", "text": source_body}],
    } in native["request"]["input"]


def test_dispatch_excludes_late_stream_shadow_after_checkpoint_result(frozen_provider_activation,
    frozen_provider_wire, dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    dispatch_manager: ContextSourceManager,
) -> None:
    """晚到的 stream shadow 不得落在已配对 result 之后破坏 seal。"""
    saver, session_id, turn_id, _sessions = dispatch_session
    model_call_id = "model-call-late-shadow"
    call_id = "call-late-shadow"
    common = {
        "turn_id": turn_id,
        "turn_scope": TurnScope.TURN_MEMBER,
        "status": CanonicalItemStatus.COMPLETED,
    }
    checkpoint_call = CanonicalItemRecord.create(
        item_sequence=1,
        item_id=f"item-lc_run--{model_call_id}",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind="tool_call",
        producer_ref={"producer_kind": "provider", "producer_id": model_call_id},
        payload={"tool_calls": [{"id": call_id, "name": "glob", "args": {}}]},
        projection_identity=ItemProjectionIdentity(projection_message_id=f"lc_run--{model_call_id}", model_call_id=model_call_id),
        wire_role="assistant",
        **common,
    )
    result = CanonicalItemRecord.create(
        item_sequence=2,
        item_id="item-result-late-shadow",
        semantic_kind=SemanticKind.TOOL_RESULT,
        payload_kind="tool_result",
        producer_ref={"producer_kind": "tool", "producer_id": call_id},
        payload={
            "tool_call_id": call_id,
            "result_id": "result-late-shadow",
            "name": "glob",
            "content": "[]",
            "tool_outcome": "success"
        , "execution_confirmed": True},
        projection_identity=ItemProjectionIdentity(projection_message_id="result-late-shadow", model_call_id=model_call_id),
        wire_role="tool",
        **common,
    )
    late_stream_call = CanonicalItemRecord.create(
        item_sequence=3,
        item_id="item-stream-call-late-shadow",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind="tool_call",
        producer_ref={"producer_kind": "provider", "producer_id": model_call_id},
        payload={
            "tool_call_id": f"{model_call_id}:tool-call:{call_id}",
            "name": "glob",
            "args": {},
        },
        projection_identity=ItemProjectionIdentity(model_call_id=model_call_id, block_id=f"{model_call_id}:tool-call:{call_id}", block_index=0),
        wire_role="assistant",
        **common,
    )
    saver.append_items(session_id, (checkpoint_call, result), thread_id=saver._storage.storage.main_thread_id(session_id))
    saver.append_items(session_id, (late_stream_call,), thread_id=saver._storage.storage.main_thread_id(session_id))

    prepared = saver.prepare_context_for_provider(session_id, saver._storage.storage.main_thread_id(session_id), turn_id=turn_id, **frozen_provider_wire('contract-provider-v2', 'chat_completions'),  plan_creation_idempotency_key='late-shadow:create', seal_idempotency_key='late-shadow:seal', execution_id=saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id=turn_id), context_source_manager=dispatch_manager, tool_set_supplier=lambda: EMPTY_TOOL_SET, **frozen_provider_activation(saver, plan_creation_idempotency_key='late-shadow:create'))

    selected_ids = {
        entry.ref.ref_id for entry in prepared["plan"].selection if entry.included
    }
    assert checkpoint_call.item_id in selected_ids
    assert result.item_id in selected_ids
    assert late_stream_call.item_id not in selected_ids


def test_composition_preserves_checkpoint_supersession_for_tool_call(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
) -> None:
    """stream 去重不能绕过 Saver 原有的 checkpoint supersession 规则。"""
    saver, session_id, turn_id, _sessions = dispatch_session
    previous_message_id = "message-superseded-tool-call"
    old_call = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-superseded-tool-call",
        semantic_kind=SemanticKind.TOOL_CALL,
        payload_kind=PayloadKind.TOOL_CALL,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "model-call-old"},
        payload={"tool_calls": [{"id": "call-old", "name": "glob", "args": {}}]},
        projection_identity=ItemProjectionIdentity(projection_message_id=previous_message_id),
        turn_id=turn_id,
        turn_scope=TurnScope.TURN_MEMBER,
        wire_role="assistant",
    )
    final = CanonicalItemRecord.create(
        item_sequence=2,
        item_id="item-final-after-tool-call",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "model-call-final"},
        payload="最终回答",
        projection_identity=ItemProjectionIdentity(projection_message_id="message-final-after-tool-call"), provenance_relations=(ItemProvenanceRelation("replaces", "projection_message", previous_message_id),),
        turn_id=turn_id,
        turn_scope=TurnScope.TURN_MEMBER,
        wire_role="assistant",
    )
    saver.append_items(session_id, (old_call, final), thread_id=saver._storage.storage.main_thread_id(session_id))

    plan = saver.compose_committed_context_plan(session_id, saver._storage.storage.main_thread_id(session_id), plan_id='checkpoint-supersession-plan', include_pending_notices=False, tool_set=EMPTY_TOOL_SET)

    ref_ids = {ref.ref_id for ref in plan.refs}
    assert old_call.item_id not in ref_ids
    assert final.item_id in ref_ids


@pytest.mark.parametrize(
    "case",
    [
        "canonical",
        "unknown",
        "omitted",
        "unavailable",
        "session",
        "assembly",
        "untyped",
        "thread",
        "ref_type",
        "unqualified",
        "raw_id",
    ],
)
def test_composer_rejects_detail_binding_outside_included_request_scope(
    dispatch_session, case
):
    saver, session_id, turn_id, _sessions = dispatch_session
    saver.register_context_contribution(session_id, saver._storage.storage.main_thread_id(session_id), _prompt('detail-scope-policy'))
    plan = saver.compose_committed_context_plan(session_id, saver._storage.storage.main_thread_id(session_id), plan_id='detail-scope-plan', tool_set=EMPTY_TOOL_SET)
    request_ref = next(ref for ref in plan.refs if ref.ref_type == "request_only")
    canonical_ref = next(ref for ref in plan.refs if ref.ref_type == "canonical_item")
    identity = selection_ref_identity(request_ref)
    detail = DetailRef(session_id, plan.thread_id, "detail-scope-assembly", "detail")
    omitted = ()
    if case == "canonical":
        identity = selection_ref_identity(canonical_ref)
    elif case == "unknown":
        identity = ("request_only", "not-in-plan", plan.thread_id)
    elif case == "omitted":
        omitted = (selection_ref_identity(request_ref),)
    elif case == "unavailable":
        plan = replace(
            plan,
            refs=tuple(
                replace(ref, availability="unavailable") if ref == request_ref else ref
                for ref in plan.refs
            ),
        )
    elif case == "session":
        detail = DetailRef(
            "foreign-session", plan.thread_id, "detail-scope-assembly", "detail"
        )
    elif case == "assembly":
        detail = DetailRef(session_id, plan.thread_id, "foreign-assembly", "detail")
    elif case == "untyped":
        detail = "detail"
    elif case == "thread":
        identity = (request_ref.ref_type, request_ref.ref_id, "foreign-thread")
    elif case == "ref_type":
        identity = ("canonical_item", request_ref.ref_id, request_ref.thread_id)
    elif case == "unqualified":
        identity = (request_ref.ref_type, request_ref.ref_id)
    elif case == "raw_id":
        identity = request_ref.ref_id
    root = saver._storage.storage.root(session_id)
    before = (root / "rollout.jsonl").read_bytes()
    with pytest.raises(
        (TypeError, ValueError), match="source-mismatch|plan-order-integrity"
    ):
        ContextPlanComposer().assembly(plan=plan, session_id=session_id, assembly_id='detail-scope-assembly', turn_id=turn_id, execution_id='unused-preflight-execution', provider_version='scope-test', request_detail_refs={identity: detail}, omitted_ref_identities=omitted, thread_id=plan.thread_id)
    assert (root / "rollout.jsonl").read_bytes() == before


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_dispatch_preserves_overlay_and_excludes_previous_prompt( frozen_provider_activation,
    frozen_provider_wire, dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    dispatch_manager: ContextSourceManager,
    target_format: str,
) -> None:
    saver, session_id, turn_id, sessions = dispatch_session
    thread_id = saver._storage.storage._path_resolver.main_thread_id(session_id)
    saver.register_context_contribution(session_id, saver._storage.storage.main_thread_id(session_id), _prompt('previous-prompt'))
    saver.register_source_overlay(
        SimpleNamespace(
            overlay_id="policy-overlay",
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_ns="",
            source_kind="workspace_policy",
            source_revision="policy-v2",
            source_overlay_epoch=1,
            base_ref="policy-base-ref",
            delta_ref="policy-delta-ref",
            base_source_revision="policy-v1",
            delta_source_revision="policy-v2",
            delta_from_revision="policy-v1",
            delta_to_revision="policy-v2",
            delta_diff_hash="policy-diff",
            status="active",
            idempotency_key="policy-overlay-key",
        ),
        session_id=session_id,
        thread_id=thread_id,
        base_content=[{"type": "text", "text": "基础规则"}],
        delta_content=[{"type": "text", "text": "本轮规则变化"}],
    )
    dispatch_manager.observe("agents:dispatch", "current-prompt", revision="current-prompt")
    prepared = saver.prepare_context_for_provider(session_id, saver._storage.storage.main_thread_id(session_id), turn_id=turn_id, **frozen_provider_wire('contract-provider-v2', target_format),  plan_creation_idempotency_key='test_context_dispatch:132:create', seal_idempotency_key='test_context_dispatch:132:seal', execution_id=saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id=turn_id), context_source_manager=dispatch_manager, tool_set_supplier=lambda: EMPTY_TOOL_SET, **frozen_provider_activation(saver, plan_creation_idempotency_key='test_context_dispatch:132:create'))
    plan = prepared["plan"]
    assert isinstance(plan, ContextRequestPlan)
    assert {
        entry.contribution_id for entry in plan.selection if entry.contribution_id
    } == {
        "source-full:agents:dispatch:current-prompt",
        "overlay:policy-overlay:base",
        "overlay:policy-overlay:delta",
    }
    assert [
        entry.ref.ref_id
        for entry in plan.selection
        if entry.selection_kind.startswith("overlay_")
    ] == ["policy-base-ref", "policy-delta-ref"]
    assert prepared["losses"] == ()

    # 重启后重新 dispatch，overlay 必须从持久 source/detail 恢复，不能依赖旧 ledger。
    restarted = RolloutCheckpointSaver(sessions)
    restarted_manager = ContextSourceManager(
        owner=dispatch_manager._owner, control_state_port=restarted,
    )
    restarted_manager.register(dispatch_manager.descriptors()[0])
    restarted_manager.observe("agents:dispatch", "current-prompt", revision="current-prompt")
    next_request = restarted.prepare_context_for_provider(session_id, restarted._storage.storage.main_thread_id(session_id), turn_id=turn_id, **frozen_provider_wire('contract-provider-v2', target_format),  plan_creation_idempotency_key='test_context_dispatch:151:create', seal_idempotency_key='test_context_dispatch:151:seal', execution_id=restarted.execution_for_turn(session_id, thread_id=restarted._storage.storage.main_thread_id(session_id), turn_id=turn_id), context_source_manager=restarted_manager, tool_set_supplier=lambda: EMPTY_TOOL_SET, **frozen_provider_activation(restarted, plan_creation_idempotency_key='test_context_dispatch:151:create'))
    next_plan = next_request["plan"]
    assert isinstance(next_plan, ContextRequestPlan)
    # 同 epoch 保留已封存的当前来源；旧 registry prompt 始终不进入 selection。
    assert {item.contribution_id for item in next_plan.contributions} == {
        "source-full:agents:dispatch:current-prompt",
        "overlay:policy-overlay:base",
        "overlay:policy-overlay:delta",
    }
    assert next_request["losses"] == ()
    assert [
        entry.ref.ref_id for entry in next_plan.selection
        if entry.selection_kind.startswith("overlay_")
    ] == ["policy-base-ref", "policy-delta-ref"]
    original = saver.get_context_assembly(
        session_id, thread_id=thread_id, assembly_id=plan.assembly_id,
    )
    restored = restarted.get_context_assembly(
        session_id, thread_id=thread_id, assembly_id=next_plan.assembly_id,
    )
    assert [frame.frame_bytes() for frame in restored.seal_binding.prefix.state.frames] == [
        frame.frame_bytes() for frame in original.seal_binding.prefix.state.frames
    ]
    assert next_request["messages"] == prepared["messages"]
    assert any(
        entry.included and entry.contribution_id == "source-full:agents:dispatch:current-prompt"
        for entry in original.selection
    )
    assert any(
        frame.item_ref.ref_id == "source-full:agents:dispatch:current-prompt"
        for frame in original.seal_binding.prefix.state.frames
    )
    if target_format == "responses":
        first_native = saver.project_context_plan_to_native(session_id, thread_id, plan)
        second_native = restarted.project_context_plan_to_native(session_id, thread_id, next_plan)
        assert first_native["request"] == second_native["request"]
        assert first_native["request"]["input"][-2:] == [
            {"role": "user", "content": [{"type": "input_text", "text": text}]}
            for text in ("基础规则", "本轮规则变化")
        ]


def test_responses_rejects_non_text_overlay_before_assembly_commit(frozen_provider_activation,
    frozen_provider_wire, dispatch_session, dispatch_manager,
) -> None:
    saver, session, turn, _sessions = dispatch_session
    thread = saver._storage.storage.main_thread_id(session)
    saver.register_source_overlay(
        SimpleNamespace(
            session_id=session, thread_id=thread, checkpoint_ns="",
            overlay_id="unsupported-overlay", source_kind="workspace_policy",
            source_revision="policy-v1", source_overlay_epoch=0,
            base_ref="unsupported-base", delta_ref=None,
            status="active", idempotency_key="unsupported-overlay-key",
        ),
        session_id=session, thread_id=thread, base_content={"policy": "原始对象"},
    )
    dispatch_manager.observe("agents:dispatch", "current-prompt", revision="current-prompt")
    pending = dispatch_manager.prepare_pending()
    with pytest.raises(ValueError, match="native request source 包含无法编码"):
        saver.prepare_context_for_provider(
            session, thread, turn_id=turn,
            execution_id=saver.execution_for_turn(session, thread_id=thread, turn_id=turn),
            **frozen_provider_wire("contract-provider-v2", "responses"),
            plan_creation_idempotency_key="unsupported:create",
            seal_idempotency_key="unsupported:seal",
            context_source_manager=dispatch_manager, tool_set_supplier=lambda: EMPTY_TOOL_SET,
            **frozen_provider_activation(saver, plan_creation_idempotency_key='unsupported:create'),
        )
    assert saver.list_context_assemblies(session, thread_id=thread) == ()
    assert dispatch_manager.prepare_pending() == pending


def test_gc_protects_sealed_selection_detail_even_with_stale_candidate( frozen_provider_activation,
    frozen_provider_wire, dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    dispatch_manager: ContextSourceManager,
) -> None:
    saver, session_id, turn_id, sessions = dispatch_session
    dispatch_manager.observe("agents:dispatch", "retained-policy", revision="retained-policy")
    prepared = saver.prepare_context_for_provider(session_id, saver._storage.storage.main_thread_id(session_id), turn_id=turn_id, **frozen_provider_wire('contract-provider-v2', 'chat_completions'),  plan_creation_idempotency_key='test_context_dispatch:170:create', seal_idempotency_key='test_context_dispatch:170:seal', execution_id=saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id=turn_id), context_source_manager=dispatch_manager, tool_set_supplier=lambda: EMPTY_TOOL_SET, **frozen_provider_activation(saver, plan_creation_idempotency_key='test_context_dispatch:170:create'))
    plan = prepared["plan"]
    assert isinstance(plan, ContextRequestPlan)
    entry = next(entry for entry in plan.selection if entry.contribution_id)
    detail_ref = entry.detail_ref
    assert isinstance(detail_ref, DetailRef)
    manifest = saver._storage.assemblies.get_context_plan_detail(session_id, saver._storage.storage.main_thread_id(session_id), detail_ref=detail_ref)
    session = get_session_path_resolver(sessions).resolve_thread_node(
        session_id, saver._storage.storage.main_thread_id(session_id),
    )
    target = session / str(manifest["relative_path"])
    assert saver._storage.assemblies.context_assembly_references_detail(session_id, saver._storage.storage.main_thread_id(session_id), assembly_id=str(plan.assembly_id), detail_ref=detail_ref)
    expired = datetime.now(UTC) + timedelta(days=365)
    assert saver.gc_context_plan_details(session_id, saver._storage.storage.main_thread_id(session_id), expired_before=expired) == ()
    assert target.is_file()

    # 模拟 GC 候选检查后发生 seal：即便候选已过时，tombstone 写锁内仍须保护引用。
    monkeypatch.setattr(
        saver._storage.assemblies,
        "context_assembly_references_detail",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(RuntimeError, match="detail-retention-protected"):
        saver.gc_context_plan_details(session_id, saver._storage.storage.main_thread_id(session_id), expired_before=expired)
    assert target.is_file()
    restored = RolloutCheckpointSaver(sessions)._storage.assemblies.get_context_plan_detail(session_id, RolloutCheckpointSaver(sessions)._storage.storage.main_thread_id(session_id), detail_ref=detail_ref)
    assert restored["status"] == restored["availability"] == "available"


def test_same_plan_and_source_ids_stay_with_their_session_owner(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    session_bundle_factory: Callable[[Path, str], Path],
) -> None:
    saver, first_session, first_turn, sessions = dispatch_session
    second_session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, second_session)
    second_acceptance = saver.accept_turn(
        second_session,
        thread_id=saver._storage.storage.main_thread_id(second_session), accepted_ingress_id="second-ingress",
        acceptance_idempotency_key="second-acceptance",
        job_id="second-job",
        admission_idempotency_key="second-admission",
        payload="独立会话",
    )
    plans: list[ContextRequestPlan] = []
    turns = {
        first_session: first_turn,
        second_session: str(second_acceptance["turn_id"]),
    }
    for session_id in (first_session, second_session):
        body = f"当前策略仅属于 {session_id}"
        saver.register_context_contribution(session_id, saver._storage.storage.main_thread_id(session_id), ContextContribution(contribution_id='same-source-id', source_kind='workspace_policy', source_revision='same-revision', body=body, content_hash=contribution_content_hash('prompt', body)), request_content=body)
    # 在两个 registry 都登记后才生成计划，避免“先读再覆盖”的测试假隔离。
    for session_id in (first_session, second_session):
        thread_id = saver._storage.storage.main_thread_id(session_id)
        execution_id = saver.execution_for_turn(
            session_id, thread_id=thread_id, turn_id=turns[session_id],
        )
        admission = saver.execution_admission(
            session_id, thread_id=thread_id, execution_id=execution_id,
        )
        with (
            session_thread_owner_scope(
                MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id,
            ),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            plan = saver.compose_committed_context_plan(session_id, thread_id, plan_id='same-plan-id', tool_set=EMPTY_TOOL_SET)
            plans.append(plan)
            assert plan.session_id == plan.to_dict()["session_id"] == session_id
            assert {ref.session_id for ref in plan.refs} == {session_id}
            request_refs = [ref for ref in plan.refs if ref.ref_type == "request_only"]
            assert len(request_refs) == 1
            assert request_refs[0].plan_id == "same-plan-id"
            assert all(
                ref.plan_id is None for ref in plan.refs if ref.ref_type == "canonical_item"
            )
            assert [item.content_hash for item in plan.contributions] == [
                contribution_content_hash("prompt", f"当前策略仅属于 {session_id}")
            ]
            # draft 仅保留 source manifest；真实正文隔离要从封存详情重启恢复后验证。
            registered = saver.create_context_plan(
                session_id,
                replace(plan, plan_creation_idempotency_key="same-plan-create"),
            )
            snapshot = saver.seal_context_plan(session_id, thread_id, registered.draft, wire_profile=None, wire_params=None, turn_id=turns[session_id], seal_idempotency_key='same-plan-seal', execution_id=execution_id, provider_version='owner-isolation-contract')
            restored = RolloutCheckpointSaver(sessions).project_context_plan_to_native(session_id, thread_id, snapshot.as_sealed_plan())
            wire = json.dumps(restored, ensure_ascii=False)
            assert session_id in wire
            other_session = second_session if session_id == first_session else first_session
            assert other_session not in wire
    assert plans[0].refs != plans[1].refs
    assert plans[0].plan_hash() != plans[1].plan_hash()


@pytest.mark.parametrize(
    "column,value",
    [
        ("detail_kind", "assembly_snapshot"),
        ("retention_class", "assembly_audit"),
        ("visibility", "private"),
        ("content_length", 9999),
    ],
)
def test_detail_manifest_mismatch_prevents_assembly_commit(frozen_provider_activation,
    frozen_provider_wire, dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: object,
    dispatch_manager: ContextSourceManager,
) -> None:
    saver, session_id, turn_id, _ = dispatch_session
    activation = frozen_provider_activation(saver, plan_creation_idempotency_key='test_context_dispatch:279:create')
    before = saver._storage.storage.jsonl_path(session_id).read_bytes()
    with saver._storage.storage._connect(
        saver._storage.storage.main_thread_id(session_id), "", session_id=session_id, read_only=True,
    ) as connection:
        before_details = connection.execute(
            "SELECT * FROM context_plan_details ORDER BY detail_ref"
        ).fetchall()
    register = saver._storage.assemblies._register_context_plan_detail

    def register_with_corrupt_manifest(connection, record: DetailRecord, **owner) -> None:
        register(connection, record, **owner)
        if record.detail_kind == "request_source":
            connection.execute(
                f"UPDATE context_plan_details SET {column} = ? WHERE detail_ref = ?",
                (value, detail_ref_key(record.detail_ref)),
            )

    monkeypatch.setattr(
        saver._storage.assemblies, "_register_context_plan_detail", register_with_corrupt_manifest
    )
    dispatch_manager.observe("agents:dispatch", "preflight-policy", revision="preflight-policy")
    with pytest.raises(
        RuntimeError, match="source-mismatch: sealed assembly detail source manifest 不一致"
    ):
        saver.prepare_context_for_provider(session_id, saver._storage.storage.main_thread_id(session_id), turn_id=turn_id, **frozen_provider_wire('detail-preflight-contract', 'chat_completions'),  plan_creation_idempotency_key='test_context_dispatch:279:create', seal_idempotency_key='test_context_dispatch:279:seal', execution_id=saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id=turn_id), context_source_manager=dispatch_manager, tool_set_supplier=lambda: EMPTY_TOOL_SET, **activation)
    assert saver._storage.storage.jsonl_path(session_id).read_bytes() == before
    with saver._storage.storage._connect(
        saver._storage.storage.main_thread_id(session_id),
        "",
        read_only=True,
        session_id=session_id,
    ) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM context_assemblies"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind = 'assembly_sealed'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT * FROM context_plan_details ORDER BY detail_ref"
        ).fetchall() == before_details
