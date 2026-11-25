"""已提交 internal execution item → 独立执行历史 DTO；不执行 I/O。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.domain.itemized.enums import ControlOutcome, PayloadKind, SemanticKind
from app.domain.itemized.records import RuntimeNoticePayload
from app.schemas.internal_v2.turn import (
    InternalDisplayProvenanceDTO,
    InternalExecutionDisplayItemDTO,
    InternalExecutionHistoryDTO,
    ToolExecutionSummaryDTO,
    TurnResponseItemDTO,
)
from app.services.mapping.itemized.history_turns.projection import (
    HistoryProjectionBudget,
    bounded_response_item,
)
from app.services.mapping.turn_response_items import (
    activity_items_from_projection,
    response_item_order,
    response_items_from_internal_item,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        InternalExecutionHistoryEntry,
    )


@dataclass(slots=True)
class _InternalExecutionHistoryProjection:
    source: InternalExecutionHistoryEntry
    outcome: str
    display_items: list[InternalExecutionDisplayItemDTO] = field(default_factory=list)
    response_items: list[TurnResponseItemDTO] = field(default_factory=list)


def build_internal_execution_history(
    entries: tuple[InternalExecutionHistoryEntry, ...],
    *,
    include: tuple[str, ...],
    budget: HistoryProjectionBudget,
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
        execution = ToolExecutionSummaryDTO.model_validate(entry.tool_execution.to_dict()) if entry.tool_execution is not None else None
        if entry.activity is not None:
            activity = [{**member, **(entry.coordinates or {}),
                         "tool_execution": entry.tool_execution.to_dict() if entry.tool_execution is not None else None,
                         "tool_outcome": entry.tool_outcome,
                         "elapsed_ms": entry.elapsed_ms if index == 0 else 0}
                for index, member in enumerate(entry.activity["activity_items"])]
            projection.response_items.extend(activity_items_from_projection({"activity_items": activity},
                mode="summary", include=fields | {"activity_outline"}, tool_call_ids=None))
            continue
        if item is None:
            raise RuntimeError("internal history 缺少请求的正文或轮廓")
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
                projection.display_items.append(
                    InternalExecutionDisplayItemDTO(
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
        parts = response_items_from_internal_item(
                item,
                include=fields,
                logical_item_ordinal=entry.logical_item_ordinal,
                elapsed_ms=entry.elapsed_ms,
            )
        projection.response_items.extend(part.model_copy(update={"tool_execution": execution, "tool_outcome": entry.tool_outcome, "source":part.source.model_copy(
            update=entry.coordinates or {})}) for part in parts)

    # 与同页 Turn 消费同一个正文预算；保留全部成员身份和明确截断事实。
    for projection in projections.values():
        projection.response_items = [bounded_response_item(part, budget) for part in projection.response_items]

    return [
        InternalExecutionHistoryDTO(
            execution_id=projection.source.execution_id,
            job_id=projection.source.job_id,
            session_id=projection.source.session_id,
            thread_id=projection.source.thread_id,
            status=projection.outcome,
            failure=projection.source.failure,
            terminal_commit_id=projection.source.terminal_commit_id,
            created_at=projection.source.created_at,
            updated_at=projection.source.updated_at,
            display_items=sorted(
                projection.display_items,
                key=lambda part: part.logical_item_ordinal,
            ),
            response_items=sorted(
                projection.response_items,
                key=response_item_order,
            ),
        )
        for projection in projections.values()
    ]
