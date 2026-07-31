from __future__ import annotations

from app.schemas.public_v2.turn import (
    TurnDetailDTO,
    TurnSummaryDTO,
    TurnUserMessageSummaryDTO,
)

TURN_RESPONSE_PREVIEW_CHARS = 1000
TURN_USER_PREVIEW_CHARS = 500
TURN_SUMMARY_SOURCE_LIMIT = 32
TURN_SUMMARY_USER_MESSAGE_LIMIT = 8


def to_turn_summary(turn: TurnDetailDTO) -> TurnSummaryDTO:
    source_ids = turn.source_message_ids[:TURN_SUMMARY_SOURCE_LIMIT]
    merged_ids = turn.merged_job_ids[:TURN_SUMMARY_SOURCE_LIMIT]
    user_messages = turn.user_messages[:TURN_SUMMARY_USER_MESSAGE_LIMIT]
    response_preview = turn.final_response[:TURN_RESPONSE_PREVIEW_CHARS]
    return TurnSummaryDTO(
        **turn.model_dump(
            exclude={
                "items_view",
                "source_message_ids",
                "merged_job_ids",
                "user_messages",
                "response_preview",
                "preview_truncated",
                "final_response",
                "items",
            }
        ),
        source_message_ids=source_ids,
        source_message_count=len(turn.source_message_ids),
        merged_job_ids=merged_ids,
        merged_job_count=len(turn.merged_job_ids),
        sources_truncated=(
            len(source_ids) < len(turn.source_message_ids)
            or len(merged_ids) < len(turn.merged_job_ids)
        ),
        user_messages=[
            TurnUserMessageSummaryDTO(
                message_id=message.message_id,
                preview=message.content[:TURN_USER_PREVIEW_CHARS],
                content_truncated=len(message.content) > TURN_USER_PREVIEW_CHARS,
                attachment_count=len(message.attachments),
                created_at=message.created_at,
            )
            for message in user_messages
        ],
        user_message_count=len(turn.user_messages),
        user_messages_truncated=len(user_messages) < len(turn.user_messages),
        response_preview=response_preview,
        preview_truncated=len(turn.final_response) > TURN_RESPONSE_PREVIEW_CHARS,
        item_count=len(turn.items),
    )
