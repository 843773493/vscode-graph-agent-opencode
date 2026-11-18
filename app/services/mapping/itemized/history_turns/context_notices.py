"""内部提示只投影身份及首次派发位置，正文由 Item inspect 按需读取。"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from app.schemas.internal_v2.turn import TurnResponsePartDTO, TurnResponseSourceDTO

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.catalog.context_notices import (
        ContextNoticeMetadata,
    )


def context_notice_parts(rows: list[ContextNoticeMetadata]) -> dict[str, list[TurnResponsePartDTO]]:
    owners: dict[str, list[TurnResponsePartDTO]] = defaultdict(list)
    for row in rows:
        owners[row.turn_id or row.execution_id].append(TurnResponsePartDTO(
            part_id=f"{row.item_id}:context", kind="context_notice", projection="summary",
            source=TurnResponseSourceDTO(message_sequence=0, item_id=row.item_id,
                item_sequence=row.item_sequence, logical_item_ordinal=row.logical_item_ordinal,
                part_ordinal=0, created_at=row.created_at, elapsed_ms=0),
            context_kind=row.kind, context_source_id=row.source_id,
            context_model_call_id=row.model_call_id, context_before_item_id=row.before_item_id,
            truncated=row.truncated,
        ))
    return dict(owners)
