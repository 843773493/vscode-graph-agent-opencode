"""已提交 internal execution item → 独立执行历史 DTO；不执行 I/O。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.domain.itemized.enums import ControlOutcome, PayloadKind, SemanticKind
from app.domain.itemized.records import RuntimeNoticePayload
from app.schemas.internal_v2.turn import (
    InternalDisplayProvenanceDTO,
    InternalExecutionDisplayPartDTO,
    InternalExecutionHistoryDTO,
    TurnResponsePartDTO,
)
from app.services.mapping.turn_response_parts import (
    response_part_order,
    response_parts_from_internal_item,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        InternalExecutionHistoryEntry,
    )


@dataclass(slots=True)
class _InternalExecutionHistoryProjection:
    source: InternalExecutionHistoryEntry
    outcome: str
    display_parts: list[InternalExecutionDisplayPartDTO] = field(default_factory=list)
    response_parts: list[TurnResponsePartDTO] = field(default_factory=list)


def build_internal_execution_history(
    entries: tuple[InternalExecutionHistoryEntry, ...],
    *,
    include: tuple[str, ...],
) -> list[InternalExecutionHistoryDTO]:
    projections: dict[str, _InternalExecutionHistoryProjection] = {}
    fields = frozenset(include)
    for entry in entries:
        outcome = ControlOutcome(entry.outcome)
        projection = projections.get(entry.execution_id)
        if projection is None:
            projection = _InternalExecutionHistoryProjection(
                source=entry,
                outcome=outcome.value,
            )
            projections[entry.execution_id] = projection
        elif (
            projection.source.job_id != entry.job_id
            or projection.source.session_id != entry.session_id
            or projection.source.thread_id != entry.thread_id
            or projection.outcome != outcome.value
            or projection.source.created_at != entry.created_at
            or projection.source.updated_at != entry.updated_at
        ):
            raise RuntimeError(
                "同一 internal execution 的 durable metadata 不一致: "
                f"{entry.execution_id}"
            )

        item = entry.item
        if (
            "internal" in fields
            and item.semantic_kind == SemanticKind.RUNTIME_NOTICE
        ):
            if (
                item.payload_kind != PayloadKind.STRUCTURED_CONTENT
                or not isinstance(item.payload, Mapping)
                or not set(item.payload) & {"prompt", "display_part"}
            ):
                continue
            notice = RuntimeNoticePayload.from_dict(item.payload)
            display_part = notice.display_part
            if (
                display_part is not None
                and display_part.kind == "generated_session_result"
            ):
                projection.display_parts.append(
                    InternalExecutionDisplayPartDTO(
                        kind=display_part.kind,
                        text=display_part.text,
                        item_id=item.item_id,
                        item_sequence=item.item_sequence,
                        logical_item_ordinal=entry.logical_item_ordinal,
                        created_at=item.created_at,
                        provenance=InternalDisplayProvenanceDTO(
                            **display_part.provenance.to_dict()
                        ),
                    )
                )
        projection.response_parts.extend(
            response_parts_from_internal_item(
                item,
                include=fields,
                logical_item_ordinal=entry.logical_item_ordinal,
                elapsed_ms=entry.elapsed_ms,
            )
        )

    return [
        InternalExecutionHistoryDTO(
            execution_id=projection.source.execution_id,
            job_id=projection.source.job_id,
            session_id=projection.source.session_id,
            thread_id=projection.source.thread_id,
            status=projection.outcome,
            created_at=projection.source.created_at,
            updated_at=projection.source.updated_at,
            display_parts=sorted(
                projection.display_parts,
                key=lambda part: part.logical_item_ordinal,
            ),
            response_parts=sorted(
                projection.response_parts,
                key=response_part_order,
            ),
        )
        for projection in projections.values()
    ]
