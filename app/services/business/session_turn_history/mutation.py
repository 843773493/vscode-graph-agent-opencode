from __future__ import annotations

from app.abstractions.turn_history import (
    TurnProjectionMutation,
    TurnProjectionPatch,
)
from app.schemas.public_v2.trace import TraceEventDTO
from app.schemas.public_v2.turn import TurnDetailDTO


def build_turn_mutation(
    current: TurnDetailDTO | None,
    updated: TurnDetailDTO,
    appended_item: TraceEventDTO | None,
) -> TurnProjectionMutation:
    """生成只包含本次语义变化的 Turn WAL mutation。"""

    if current is None:
        return TurnProjectionMutation(
            turn_id=updated.turn_id,
            base_revision=0,
            create=updated,
        )

    patch_values: dict[str, object] = {
        "revision": updated.revision,
        "updated_at": updated.updated_at,
    }
    for field_name in (
        "status",
        "completed_at",
        "source_message_ids",
        "merged_job_ids",
        "user_messages",
        "response_preview",
        "preview_truncated",
        "final_response",
    ):
        if getattr(current, field_name) != getattr(updated, field_name):
            patch_values[field_name] = getattr(updated, field_name)
    if appended_item is not None:
        patch_values["append_items"] = [appended_item]
    return TurnProjectionMutation(
        turn_id=updated.turn_id,
        base_revision=current.revision,
        patch=TurnProjectionPatch.model_validate(patch_values),
    )
