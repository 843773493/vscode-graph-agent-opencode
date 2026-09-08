from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.domain.itemized.assembly_snapshot import (
    context_request_hash,
)
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SelectionKind,
    SemanticKind,
)
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    content_hash,
    contribution_content_hash,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.migration.legacy_adapter import (
    LegacyRolloutAdapter,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    default_idempotency_key,
)
from app.services.mapping.itemized.history import project_history_plan
from app.services.mapping.itemized.langchain import project_context_plan


@pytest.fixture
def cross_language_vectors() -> dict[str, object]:
    return json.loads(
        (Path.cwd() / "tests/fixtures/itemized/hash_vectors.json").read_text(
            encoding="utf-8"
        )
    )


def test_storage_idempotency_uses_the_cross_language_golden(
    cross_language_vectors: dict[str, object],
) -> None:
    vector = next(
        row for row in cross_language_vectors["preimages"] if row["id"] == "idempotency"
    )
    call = cross_language_vectors["idempotency"]
    assert (
        default_idempotency_key(
            commit_kind=call["commit_kind"],
            subject_id=call["subject_id"],
            outcome=call["outcome"],
            metadata=vector["preimage"],
        )
        == call["key"]
    )


@pytest.fixture
def user_item() -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-user-1",
        semantic_kind=SemanticKind.USER_INPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "user",
            "producer_id": "ingress-1",
            "invocation_id": "turn-1",
        },
        payload="读取 README",
        created_at="2026-09-07T00:00:00+00:00",
        metadata={"source_revision": "rev-1"},
        turn_id="turn-1",
        turn_scope="turn_root",
        message_group_id="group-1",
        wire_role="user",
    )


def test_content_plan_request_and_idempotency_golden_vectors(
    cross_language_vectors: dict[str, object],
) -> None:
    vectors = {row["id"]: row for row in cross_language_vectors["preimages"]}
    assert content_hash("text", "golden") == (
        "sha256:jcs:v1:cc530bb3997805ec119bda25acfabb17250517cafe6522ea5550dd00d7775c6a"
    )

    item = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-golden-1",
        semantic_kind=SemanticKind.USER_INPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "user",
            "producer_id": "ingress-golden",
            "invocation_id": "turn-golden",
        },
        payload="golden input",
        created_at="2026-09-07T00:00:00+00:00",
        metadata={"source_revision": "rev-golden"},
        turn_id="turn-golden",
        turn_scope="turn_root",
        message_group_id="group-golden",
        wire_role="user",
    )
    ref = ContextRef.canonical_item(item, session_id="session-golden")
    selection = ContextSelectionEntry(
        assembly_id="assembly-golden",
        plan_ordinal=0,
        ref=ref,
        selection_kind=SelectionKind.CANONICAL_HISTORY,
        source_revision=ref.source_revision,
        content_length=ref.content_length,
        content_hash=ref.content_hash,
        visibility=ref.visibility,
        protection=ref.protection,
        availability=ref.availability,
    )
    plan = ContextRequestPlan(
        session_id="session-golden", plan_id="plan-golden", refs=(ref,)
    ).seal_for_assembly("assembly-golden", selection=(selection,))
    assert plan.plan_hash() == vectors["core-plan"]["hash"]
    assert (
        context_request_hash(plan, "provider-golden", target_format="native")
        == vectors["core-request"]["hash"]
    )
    assert default_idempotency_key(
        commit_kind="terminal_convergence",
        subject_id="turn-golden",
        outcome="completed",
        metadata={"execution_id": "execution-golden", "final_item_id": item.item_id},
    ) == (
        "terminal_convergence:turn-golden:completed:"
        "sha256:jcs:v1:1abb23c5879377c9adbe350eefbf01b95e8f8ec2554ba3ffd8df2f594da36c9e"
    )


def test_legacy_adapter_uses_the_cross_language_golden(
    cross_language_vectors: dict[str, object],
) -> None:
    rows = {row["id"]: row for row in cross_language_vectors["preimages"]}
    message = rows["legacy-message"]["preimage"]
    record = {
        name: value for name, value in message.items() if name != "source_session_id"
    }
    record.update(
        format_version=1,
        record_type="message",
        metadata={},
        turn_id="legacy-turn-golden",
    )
    candidate = LegacyRolloutAdapter(message["source_session_id"]).group_candidates(
        [record]
    )[0]
    assert candidate["candidate_status"] == "accepted"
    assert candidate["legacy_seed_hash"] == rows["legacy-seed"]["hash"]


@pytest.mark.parametrize(
    ("tool_status", "tool_outcome"),
    [("success", "success"), ("error", "failure")],
)
def test_tool_result_codec_preserves_tool_outcome_status(
    tool_status: str,
    tool_outcome: str,
) -> None:
    codec = LangChainMessageCodec()
    message = ToolMessage(
        content="工具返回",
        id=f"tool-result-{tool_status}",
        name="read_file",
        tool_call_id="call-1",
        status=tool_status,
    )
    (item,) = codec.items_for_message(
        message,
        item_sequence=1,
        message_id=str(message.id),
        turn_id="turn-1",
        timestamp="2026-09-07T00:00:00+00:00",
    )
    assert item.payload["tool_outcome"] == tool_outcome
    projected = codec.project_message((item,))
    assert projected["data"]["status"] == tool_status


def test_history_and_provider_consume_the_same_sealed_selection_order(
    user_item: CanonicalItemRecord,
) -> None:
    item = user_item
    item_ref = ContextRef.canonical_item(item, session_id="session-shared-selection")
    body = {"text": "必须先读取配置"}
    contribution = ContextContribution(
        contribution_id="contribution-shared-selection",
        source_kind="system_policy",
        source_revision="policy-rev-shared",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        content_length=len(canonical_json_bytes(body)),
        metadata={"source_ordinal": 0},
    )
    request_ref = ContextRef.request_only_ref(
        contribution.contribution_id,
        session_id="session-shared-selection",
        plan_id="plan-shared-selection",
        source_revision=contribution.source_revision,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
        source_ref="shared-policy",
    )
    canonical_selection = ContextSelectionEntry(
        assembly_id="assembly-shared-selection",
        plan_ordinal=0,
        ref=item_ref,
        selection_kind=SelectionKind.CANONICAL_HISTORY,
        source_revision=item_ref.source_revision,
        content_length=item_ref.content_length,
        content_hash=item_ref.content_hash,
        visibility=item_ref.visibility,
        protection=item_ref.protection,
        availability=item_ref.availability,
    )
    request_selection = ContextSelectionEntry(
        assembly_id="assembly-shared-selection",
        plan_ordinal=1,
        ref=request_ref,
        selection_kind=SelectionKind.REQUEST_ONLY,
        source_revision=request_ref.source_revision,
        content_length=request_ref.content_length,
        content_hash=request_ref.content_hash,
        visibility=request_ref.visibility,
        protection=request_ref.protection,
        availability=request_ref.availability,
        detail_ref=DetailRef(
            "session-shared-selection", "assembly-shared-selection", "detail-shared-selection"
        ),
        contribution_id=contribution.contribution_id,
        contribution_ordinal=0,
    )
    plan = ContextRequestPlan(
        session_id="session-shared-selection",
        plan_id="plan-shared-selection",
        refs=(item_ref, request_ref),
        contributions=(contribution,),
    ).seal_for_assembly(
        "assembly-shared-selection",
        selection=(canonical_selection, request_selection),
    )

    provider_messages = project_context_plan(
        plan,
        (item,),
        request_only_content={contribution.contribution_id: body},
    )
    history_messages = project_history_plan(plan, (item,))

    assert [
        message.id for message in provider_messages if isinstance(message, HumanMessage)
    ] == [item.item_id]
    assert any(isinstance(message, SystemMessage) for message in provider_messages)
    assert [message.id for message in history_messages] == [item.item_id]
    assert all(not isinstance(message, SystemMessage) for message in history_messages)
