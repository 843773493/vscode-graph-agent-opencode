"""消息投影不得让 Job ID 兼任 Turn 或 fork anchor。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage

from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionPartRef,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.business.reasoning_checkpoint_service import (
    _rewrite_latest_assistant_message,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_compaction import (
    ForkCompactionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.message_materializer import (
    RolloutMessageMaterializerMixin,
)


def test_job_metadata_cannot_replace_explicit_turn_identity() -> None:
    message = AIMessage(
        content="answer",
        response_metadata={"message_metadata": {"job_id": "job-real"}},
    )
    assert LangChainMessageCodec().turn_id(message, "turn-real", "message-1") == "turn-real"


def test_fork_anchor_accepts_turn_and_message_but_rejects_job_identity() -> None:
    message = AIMessage(
        content="answer",
        id="message-real",
        response_metadata={
            "message_metadata": {"turn_id": "turn-real", "job_id": "job-real"},
        },
    )
    assert ForkCompactionMixin._message_matches_anchor(message, "message-real")
    assert ForkCompactionMixin._message_matches_anchor(message, "turn-real")
    assert not ForkCompactionMixin._message_matches_anchor(message, "job-real")


def test_assistant_rebinding_does_not_inherit_or_forge_job_identity() -> None:
    messages = [
        AIMessage(
            content="old",
            response_metadata={
                "message_metadata": {
                    "internal": True,
                    "turn_id": "old-turn",
                    "job_id": "old-job",
                },
            },
        )
    ]
    rewritten = _rewrite_latest_assistant_message(
        messages,
        content_blocks=({"type": "text", "text": "final"},),
        final_text="final",
        message_id="message-final",
        message_created_at=datetime(2026, 10, 4, tzinfo=UTC),
        token_usage=None,
        turn_id="turn-real",
        preserve_content_part_refs=False,
    )
    assert rewritten is not None
    metadata = rewritten.response_metadata["message_metadata"]
    assert metadata["turn_id"] == "turn-real"
    assert "job_id" not in metadata
    assert "internal" not in metadata


@pytest.fixture
def projection_codec() -> LangChainMessageCodec:
    return LangChainMessageCodec()


@pytest.fixture
def stream_projection_item() -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id=(
            "item-execution-1-model-model-call-1-block-model-call-1:block:part-1"
        ),
        semantic_kind="assistant_output",
        payload_kind="text",
        status="completed",
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "model-call-1",
            "invocation_id": "model-call-1",
        },
        payload="真实流式回复",
        created_at=datetime(2026, 10, 7, tzinfo=UTC).isoformat(),
        projection_identity=ItemProjectionIdentity(
            model_call_id="model-call-1",
            block_id="model-call-1:block:part-1",
            block_index=0,
        ),
        turn_id="turn-real",
        turn_scope="turn_member",
        message_group_id="message-model-call-1",
        wire_role="assistant",
    )


@pytest.fixture
def stream_projection_carrier(
    projection_codec: LangChainMessageCodec,
    stream_projection_item: CanonicalItemRecord,
) -> AIMessage:
    projected = projection_codec.project_message((stream_projection_item,))
    hydrated = RolloutMessageMaterializerMixin._hydrate_message_identity(
        projected,
        message_id=(
            "execution-1-model-model-call-1-block-model-call-1:block:part-1"
        ),
        turn_id="turn-real",
        created_at=stream_projection_item.created_at,
    )
    carrier = projection_codec.from_dict(hydrated)
    assert isinstance(carrier, AIMessage)
    return carrier


def test_stream_projection_roundtrip_preserves_empty_typed_message_identity(
    projection_codec: LangChainMessageCodec,
    stream_projection_item: CanonicalItemRecord,
    stream_projection_carrier: AIMessage,
) -> None:
    original = stream_projection_item.to_dict()
    projected = projection_codec.project_message((stream_projection_item,))
    data = projected["data"]
    assert isinstance(data, dict)
    assert data["id"] == (
        "execution-1-model-model-call-1-block-model-call-1:block:part-1"
    )
    assert stream_projection_carrier.id == data["id"]

    restored = projection_codec.items_for_message(
        stream_projection_carrier,
        item_sequence=1,
        message_id=projection_codec.message_id(stream_projection_carrier, 0),
        turn_id="turn-real",
        execution_id="execution-1",
        timestamp=stream_projection_item.created_at,
    )

    assert len(restored) == 1
    identity = restored[0].projection_identity
    assert identity is not None
    assert identity.projection_message_id is None
    assert identity.model_call_id == "model-call-1"
    assert identity.block_id == "model-call-1:block:part-1"
    assert identity.block_index == 0
    assert restored[0].projection_manifest() == stream_projection_item.projection_manifest()
    assert projection_codec.project_message(restored) == projected
    assert stream_projection_item.to_dict() == original


def test_explicit_checkpoint_projection_id_survives_roundtrip(
    projection_codec: LangChainMessageCodec,
    stream_projection_item: CanonicalItemRecord,
) -> None:
    item = replace(
        stream_projection_item,
        item_id="item-message-checkpoint",
        message_group_id="message-message-checkpoint",
        projection_identity=ItemProjectionIdentity(
            projection_message_id="message-checkpoint",
            model_call_id="model-call-1",
            phase="final_answer",
            content_part_refs=(ProjectionPartRef("part-1", 0, "text"),),
        ),
    )
    projected = projection_codec.project_message((item,))
    carrier = projection_codec.from_dict(projected)
    assert isinstance(carrier, AIMessage)
    assert carrier.id == "message-checkpoint"

    restored = projection_codec.items_for_message(
        carrier,
        item_sequence=1,
        message_id=projection_codec.message_id(carrier, 0),
        turn_id="turn-real",
        execution_id="execution-1",
        timestamp=item.created_at,
    )

    assert restored[0].projection_identity == item.projection_identity
    assert restored[0].projection_manifest() == item.projection_manifest()
    assert projection_codec.project_message(restored) == projected


@pytest.mark.parametrize(
    "message_id",
    (
        "foreign-message",
        "item-execution-1-model-model-call-1-block-model-call-1:block:part-1",
    ),
)
def test_stream_projection_rejects_changed_visible_id_and_full_item_id_alias(
    projection_codec: LangChainMessageCodec,
    stream_projection_item: CanonicalItemRecord,
    stream_projection_carrier: AIMessage,
    message_id: str,
) -> None:
    original = stream_projection_item.to_dict()
    carrier = stream_projection_carrier.model_copy(update={"id": message_id})

    with pytest.raises(ValueError, match="canonical group manifest item identity"):
        projection_codec.items_for_message(
            carrier,
            item_sequence=1,
            message_id=projection_codec.message_id(carrier, 0),
            turn_id="turn-real",
            execution_id="execution-1",
            timestamp=stream_projection_item.created_at,
        )

    assert stream_projection_item.to_dict() == original


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("model_call_id", "model-call-foreign"),
        ("phase", "reasoning"),
        ("content_part_refs", [{"id": "part-foreign", "index": 0, "type": "text"}]),
    ),
)
def test_stream_projection_rejects_changed_visible_identity_fields(
    projection_codec: LangChainMessageCodec,
    stream_projection_item: CanonicalItemRecord,
    stream_projection_carrier: AIMessage,
    field: str,
    value: object,
) -> None:
    original = stream_projection_item.to_dict()
    carrier = stream_projection_carrier.model_copy(
        update={
            "response_metadata": {
                **stream_projection_carrier.response_metadata,
                field: value,
            }
        }
    )

    with pytest.raises(ValueError, match="canonical projection identity"):
        projection_codec.items_for_message(
            carrier,
            item_sequence=1,
            message_id=projection_codec.message_id(carrier, 0),
            turn_id="turn-real",
            execution_id="execution-1",
            timestamp=stream_projection_item.created_at,
        )

    assert stream_projection_item.to_dict() == original


def test_tool_group_companions_keep_empty_message_identity_after_roundtrip(
    projection_codec: LangChainMessageCodec,
) -> None:
    message = AIMessage(
        id="message-with-tool",
        content=[
            {"type": "reasoning", "reasoning": "工具前推理"},
            "工具前可见回复",
        ],
        tool_calls=[{"id": "call-1", "name": "shell", "args": {}, "type": "tool_call"}],
        response_metadata={"model_call_id": "model-call-1"},
    )
    original = projection_codec.items_for_message(
        message,
        item_sequence=1,
        message_id="message-with-tool",
        turn_id="turn-real",
        execution_id="execution-1",
        timestamp=datetime(2026, 10, 7, tzinfo=UTC).isoformat(),
    )
    projected = projection_codec.project_message(original)
    carrier = projection_codec.from_dict(projected)

    restored = projection_codec.items_for_message(
        carrier,
        item_sequence=1,
        message_id=projection_codec.message_id(carrier, 0),
        turn_id="turn-real",
        execution_id="execution-1",
        timestamp=datetime(2026, 10, 7, tzinfo=UTC).isoformat(),
    )

    assert [item.semantic_kind for item in restored] == [
        "reasoning",
        "assistant_output",
        "tool_call",
    ]
    assert [
        item.projection_identity.projection_message_id
        for item in restored
        if item.projection_identity is not None
    ] == [None, None, "message-with-tool"]
    assert [item.projection_manifest() for item in restored] == [
        item.projection_manifest() for item in original
    ]
