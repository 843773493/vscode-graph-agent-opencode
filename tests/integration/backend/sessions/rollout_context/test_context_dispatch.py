"""验证真实 Saver dispatch 选择当前 prompt 和持久 overlay 的共同来源。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.hashing import contribution_content_hash
from app.domain.itemized.mutation_intents import (
    ApplySourceLifecycleDecision,
    MutationIntentOwner,
)
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
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
)
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def dispatch_session(
    request: pytest.FixtureRequest,
    session_bundle_factory: Callable[[Path, str], Path],
) -> tuple[RolloutCheckpointSaver, str, str, Path]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"ses_{uuid4().hex}"
    session_bundle_factory(sessions, session_id)
    saver = RolloutCheckpointSaver(sessions)
    accepted = saver.accept_turn(
        session_id,
        accepted_ingress_id="dispatch-ingress",
        acceptance_idempotency_key="dispatch-key",
        payload="请基于最新策略继续工作",
        payload_kind=PayloadKind.TEXT,
    )
    return saver, session_id, str(accepted["turn_id"]), sessions


def _prompt(identity: str) -> ContextContribution:
    body = {"policy": identity}
    return ContextContribution(
        contribution_id=identity,
        source_kind="workspace_policy",
        source_revision=identity,
        body=body,
        content_hash=contribution_content_hash("prompt", body),
    )


def test_dispatch_projects_pending_source_to_chat_and_responses(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
) -> None:
    """sealed assembly 中的 source item 必须进入两种 Provider wire。"""
    saver, session_id, turn_id, _sessions = dispatch_session
    source_body = "# 源码调试工具\n\n通过固定信封调用调试目标。"
    saver.consume_mutation_intent(
        ApplySourceLifecycleDecision(
            owner=MutationIntentOwner(session_id=session_id, thread_id="main"),
            source_id="skill:debugging",
            source_kind="skill",
            name="debugging",
            decision_kind="base",
            revision="debugging-v1",
            content=source_body,
            item_id="item-context-source:skill:debugging:debugging-v1:base",
        )
    )

    prepared = saver.prepare_context_for_provider(
        session_id,
        turn_id=turn_id,
        provider_version="contract-provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="pending-source-chat:create",
        seal_idempotency_key="pending-source-chat:seal",
    )

    assert any(message.type == "human" and message.text == source_body for message in prepared["messages"])
    native = saver.project_context_plan_to_native(session_id, prepared["plan"])
    assert {
        "role": "user",
        "content": [{"type": "input_text", "text": source_body}],
    } in native["request"]["input"]


def test_dispatch_excludes_late_stream_shadow_after_checkpoint_result(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
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
        metadata={
            "execution_confirmed": True,
            "model_call_id": model_call_id,
            "projection_message_id": f"lc_run--{model_call_id}",
            "projection_group": {"ordinal": 0, "size": 1},
        },
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
            "tool_outcome": "success",
        },
        metadata={
            "execution_confirmed": True,
            "model_call_id": model_call_id,
            "projection_message_id": "result-late-shadow",
        },
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
        metadata={
            "model_call_id": model_call_id,
            "block_id": f"{model_call_id}:tool-call:{call_id}",
            "block_index": 0,
        },
        wire_role="assistant",
        **common,
    )
    saver.append_items(session_id, (checkpoint_call, result))
    saver.append_items(session_id, (late_stream_call,))

    prepared = saver.prepare_context_for_provider(
        session_id,
        turn_id=turn_id,
        provider_version="contract-provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="late-shadow:create",
        seal_idempotency_key="late-shadow:seal",
    )

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
        payload={
            "tool_calls": [{"id": "call-old", "name": "glob", "args": {}}]
        },
        metadata={
            "execution_confirmed": True,
            "projection_message_id": previous_message_id,
            "projection_group": {"ordinal": 0, "size": 1},
        },
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
        metadata={
            "projection_message_id": "message-final-after-tool-call",
            "supersedes_message_id": previous_message_id,
        },
        turn_id=turn_id,
        turn_scope=TurnScope.TURN_MEMBER,
        wire_role="assistant",
    )
    saver.append_items(session_id, (old_call, final))

    plan = saver.compose_committed_context_plan(
        session_id,
        plan_id="checkpoint-supersession-plan",
        include_pending_notices=False,
    )

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
    ],
)
def test_composer_rejects_detail_binding_outside_included_request_scope(
    dispatch_session, case
):
    saver, session_id, turn_id, _sessions = dispatch_session
    saver.register_context_contribution(session_id, _prompt("detail-scope-policy"))
    plan = saver.compose_committed_context_plan(session_id, plan_id="detail-scope-plan")
    request_ref = next(ref for ref in plan.refs if ref.ref_type == "request_only")
    canonical_ref = next(ref for ref in plan.refs if ref.ref_type == "canonical_item")
    ref_id = request_ref.ref_id
    detail = DetailRef(session_id, "detail-scope-assembly", "detail")
    omitted = ()
    if case == "canonical":
        ref_id = canonical_ref.ref_id
    elif case == "unknown":
        ref_id = "not-in-plan"
    elif case == "omitted":
        omitted = (request_ref.ref_id,)
    elif case == "unavailable":
        plan = replace(
            plan,
            refs=tuple(
                replace(ref, availability="unavailable") if ref == request_ref else ref
                for ref in plan.refs
            ),
        )
    elif case == "session":
        detail = DetailRef("foreign-session", "detail-scope-assembly", "detail")
    elif case == "assembly":
        detail = DetailRef(session_id, "foreign-assembly", "detail")
    elif case == "untyped":
        detail = "detail"
    root = saver._storage.root(session_id)
    before = (root / "rollout.jsonl").read_bytes()
    with pytest.raises(
        (TypeError, ValueError), match="source-mismatch|plan-order-integrity"
    ):
        ContextPlanComposer().assembly(
            plan=plan,
            session_id=session_id,
            assembly_id="detail-scope-assembly",
            turn_id=turn_id,
            execution_id="unused-preflight-execution",
            provider_version="scope-test",
            request_detail_refs={ref_id: detail},
            omitted_ref_ids=omitted,
        )
    assert (root / "rollout.jsonl").read_bytes() == before


def test_dispatch_preserves_overlay_and_excludes_previous_prompt(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
) -> None:
    saver, session_id, turn_id, sessions = dispatch_session
    saver.register_context_contribution(session_id, _prompt("previous-prompt"))
    saver.register_source_overlay(
        SimpleNamespace(
            overlay_id="policy-overlay",
            session_id=session_id,
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
        base_content={"policy": "基础规则"},
        delta_content={"policy": "本轮规则变化"},
    )
    prepared = saver.prepare_context_for_provider(
        session_id,
        turn_id=turn_id,
        prompt_contributions=(_prompt("current-prompt"),),
        provider_version="contract-provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="test_context_dispatch:132:create",
        seal_idempotency_key="test_context_dispatch:132:seal",
    )
    plan = prepared["plan"]
    assert isinstance(plan, ContextRequestPlan)
    assert {
        entry.contribution_id for entry in plan.selection if entry.contribution_id
    } == {
        "current-prompt",
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
    next_request = restarted.prepare_context_for_provider(
        session_id,
        turn_id=turn_id,
        provider_version="contract-provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="test_context_dispatch:151:create",
        seal_idempotency_key="test_context_dispatch:151:seal",
    )
    next_plan = next_request["plan"]
    assert isinstance(next_plan, ContextRequestPlan)
    assert {item.contribution_id for item in next_plan.contributions} == {
        "overlay:policy-overlay:base",
        "overlay:policy-overlay:delta",
    }
    assert next_request["losses"] == ()


def test_gc_protects_sealed_selection_detail_even_with_stale_candidate(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, session_id, turn_id, sessions = dispatch_session
    prepared = saver.prepare_context_for_provider(
        session_id,
        turn_id=turn_id,
        prompt_contributions=(_prompt("retained-policy"),),
        provider_version="contract-provider-v2",
        target_format="chat_completions",
        plan_creation_idempotency_key="test_context_dispatch:170:create",
        seal_idempotency_key="test_context_dispatch:170:seal",
    )
    plan = prepared["plan"]
    assert isinstance(plan, ContextRequestPlan)
    entry = next(entry for entry in plan.selection if entry.contribution_id)
    detail_ref = entry.detail_ref
    assert isinstance(detail_ref, DetailRef)
    manifest = saver._storage.get_context_plan_detail(session_id, detail_ref=detail_ref)
    session = get_session_path_resolver(sessions).resolve_session_node(session_id)
    target = session / str(manifest["relative_path"])
    assert saver._storage.context_assembly_references_detail(
        session_id,
        assembly_id=str(plan.assembly_id),
        detail_ref=detail_ref,
    )
    expired = datetime.now(UTC) + timedelta(days=365)
    assert saver.gc_context_plan_details(session_id, expired_before=expired) == ()
    assert target.is_file()

    # 模拟 GC 候选检查后发生 seal：即便候选已过时，tombstone 写锁内仍须保护引用。
    monkeypatch.setattr(
        saver._storage,
        "context_assembly_references_detail",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(RuntimeError, match="detail-retention-protected"):
        saver.gc_context_plan_details(session_id, expired_before=expired)
    assert target.is_file()
    restored = RolloutCheckpointSaver(sessions)._storage.get_context_plan_detail(
        session_id,
        detail_ref=detail_ref,
    )
    assert restored["status"] == restored["availability"] == "available"


def test_same_plan_and_source_ids_stay_with_their_session_owner(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    session_bundle_factory: Callable[[Path, str], Path],
) -> None:
    saver, first_session, first_turn, sessions = dispatch_session
    second_session = f"ses_{uuid4().hex}"
    session_bundle_factory(sessions, second_session)
    second_acceptance = saver.accept_turn(
        second_session,
        accepted_ingress_id="second-ingress",
        acceptance_idempotency_key="second-acceptance",
        payload="独立会话",
    )
    plans: list[ContextRequestPlan] = []
    turns = {
        first_session: first_turn,
        second_session: str(second_acceptance["turn_id"]),
    }
    for session_id in (first_session, second_session):
        body = f"当前策略仅属于 {session_id}"
        saver.register_context_contribution(
            session_id,
            ContextContribution(
                contribution_id="same-source-id",
                source_kind="workspace_policy",
                source_revision="same-revision",
                body=body,
                content_hash=contribution_content_hash("prompt", body),
            ),
            request_content=body,
        )
    # 在两个 registry 都登记后才生成计划，避免“先读再覆盖”的测试假隔离。
    for session_id in (first_session, second_session):
        plan = saver.compose_committed_context_plan(session_id, plan_id="same-plan-id")
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
        snapshot = saver.seal_context_plan(
            session_id,
            registered.draft,
            turn_id=turns[session_id],
            seal_idempotency_key="same-plan-seal",
            execution_id=saver.execution_for_turn(
                session_id, turn_id=turns[session_id]
            ),
            provider_version="owner-isolation-contract",
        )
        restored = RolloutCheckpointSaver(sessions).project_context_plan_to_native(
            session_id,
            snapshot.as_sealed_plan(),
        )
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
def test_detail_manifest_mismatch_prevents_assembly_commit(
    dispatch_session: tuple[RolloutCheckpointSaver, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: object,
) -> None:
    saver, session_id, turn_id, _ = dispatch_session
    before = saver._storage.jsonl_path(session_id).read_bytes()
    register = saver._storage.register_context_plan_detail

    def register_with_corrupt_manifest(record: DetailRecord) -> None:
        register(record)
        with saver._storage._connect(session_id, "") as connection:
            connection.execute(
                f"UPDATE context_plan_details SET {column} = ? WHERE detail_ref = ?",
                (value, detail_ref_key(record.detail_ref)),
            )

    monkeypatch.setattr(
        saver._storage, "register_context_plan_detail", register_with_corrupt_manifest
    )
    with pytest.raises(
        ValueError, match="source-mismatch: request source detail manifest"
    ):
        saver.prepare_context_for_provider(
            session_id,
            turn_id=turn_id,
            prompt_contributions=(_prompt("preflight-policy"),),
            provider_version="detail-preflight-contract",
            target_format="chat_completions",
            plan_creation_idempotency_key="test_context_dispatch:279:create",
            seal_idempotency_key="test_context_dispatch:279:seal",
        )
    assert saver._storage.jsonl_path(session_id).read_bytes() == before
    with saver._storage._connect(session_id, "", read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM context_assemblies"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind = 'assembly_sealed'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT availability FROM context_plan_details"
        ).fetchall() == [("unavailable",)]
