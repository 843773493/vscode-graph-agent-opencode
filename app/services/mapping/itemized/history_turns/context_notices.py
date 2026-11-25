"""内部提示只投影身份及首次派发位置，正文由 Item inspect 按需读取。"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from app.schemas.internal_v2.turn import TurnResponseItemDTO, TurnResponseSourceDTO
from app.schemas.internal_v2.turn_search import (
    CanonicalContextContentReference,
    SealedContextContentReference,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.catalog.context_notices import (
        ContextNoticeMetadata,
    )


def context_notice_parts(rows: list[ContextNoticeMetadata], *,
                         continuation: str | None = None) -> dict[str, list[TurnResponseItemDTO]]:
    owners: dict[str, list[TurnResponseItemDTO]] = defaultdict(list)
    for row in rows:
        reference = (CanonicalContextContentReference(item_id=row.item_id) if row.item_id is not None
            else SealedContextContentReference(session_id=row.session_id, thread_id=row.thread_id,
                assembly_id=row.assembly_id, contribution_id=row.contribution_id))
        owners[row.turn_id or row.execution_id].append(TurnResponseItemDTO(
            id=f"{row.model_call_id}:{row.plan_ordinal}:context", kind="context_notice", projection="summary",
            source=TurnResponseSourceDTO(message_sequence=0, item_id=row.item_id,
                item_sequence=row.item_sequence, logical_item_ordinal=row.logical_item_ordinal,
                model_call_id=row.model_call_id, created_at=row.created_at, elapsed_ms=0,
                turn_id=row.turn_id, execution_id=row.execution_id),
            context_kind=row.kind, context_source_id=row.source_id,
            context_model_call_id=row.model_call_id, context_before_item_id=row.before_item_id,
            context_content_ref=reference, context_source_revision=row.source_revision,
            context_content_length=row.content_length, context_availability=row.availability,
            context_anchor=row.anchor, context_anchor_ordinal=row.anchor_ordinal,
            context_plan_ordinal=row.plan_ordinal,
            context_cursor=continuation if row is rows[-1] else None,
        ))
    return dict(owners)
