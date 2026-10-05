"""消息投影不得让 Job ID 兼任 Turn 或 fork anchor。"""

from datetime import UTC, datetime

from langchain_core.messages import AIMessage

from app.services.business.reasoning_checkpoint_service import (
    _rewrite_latest_assistant_message,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_compaction import (
    ForkCompactionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
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
