"""通过统一 reader 按 snapshot 和 include 查询历史；不再持有 DTO 转换。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage

from app.core.history_loading import (
    DEFAULT_INITIAL_INCLUDE,
    HistoryLoadingConfig,
    default_history_loading_config,
)
from app.domain.itemized.enums import ControlOutcome, PayloadKind, SemanticKind
from app.domain.itemized.records import RuntimeNoticePayload
from app.schemas.internal_v2.turn import (
    InternalDisplayProvenanceDTO,
    InternalExecutionDisplayPartDTO,
    InternalExecutionHistoryDTO,
    TurnDetailDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnResponsePartDTO,
    TurnSummaryDTO,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    ContextChain,
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    InternalExecutionHistoryEntry,
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text
from app.services.infrastructure.rollout_history.paging import (
    HistoryPageReadMixin,
)
from app.services.infrastructure.rollout_history.snapshot import (
    IndexedHistorySnapshots,
    IndexedTurnSpan,
)
from app.services.infrastructure.turn_history.load_plan import (
    DetailReadBudget,
    LoadLimits,
)
from app.services.infrastructure.turn_history.models import (
    InvalidTurnCursorError,
    StaleTurnReferenceError,
)
from app.services.mapping.itemized.history_turns.detail import build_detail
from app.services.mapping.itemized.history_turns.projection import (
    project_detail,
    summary,
)
from app.services.mapping.turn_response_parts import (
    response_part_order,
    response_parts_from_internal_item,
    response_parts_from_records,
)


@dataclass(slots=True)
class _InternalExecutionHistoryProjection:
    execution_id: str
    job_id: str
    session_id: str
    thread_id: str
    outcome: str
    created_at: str
    updated_at: str
    display_parts: list[InternalExecutionDisplayPartDTO] = field(default_factory=list)
    response_parts: list[TurnResponsePartDTO] = field(default_factory=list)


class RolloutHistoryReader(HistoryPageReadMixin):
    def __init__(self, context_reader: RolloutContextReader) -> None:
        self._context_reader = context_reader
        self._snapshots = IndexedHistorySnapshots(context_reader)

    def bootstrap(
        self,
        session_id: str,
        thread_id: str,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> tuple[TurnSummaryDTO | None, str | None, int]:
        indexed = self._snapshots.read(session_id, thread_id)
        if indexed is None:
            return None, None, 1
        try:
            rollout_id = indexed.rollout_id
            projection_epoch = indexed.projection_epoch
            snapshot = indexed.snapshot
            if indexed.view_id is None:
                return None, None, projection_epoch
            configured = policy or default_history_loading_config()
            bounds = self._context_reader.read_history_snapshot_bounds(
                snapshot,
                indexed.chain,
            )
            if (
                bounds.view_id != indexed.view_id
                or bounds.history_view_revision
                != snapshot.manifest.history_view_revision
            ):
                raise RuntimeError("bootstrap history bounds 与 snapshot owner 不一致")
            if bounds.upper_turn_ordinal is None:
                return None, None, projection_epoch
            raw_spans, _ = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="tail",
                anchor_ordinal=None,
                upper_ordinal=bounds.upper_turn_ordinal,
                limit=1,
            )
            if not raw_spans:
                return None, None, projection_epoch
            span = self._span_from_row(raw_spans[0])
            # Turn/root 已由 SQL 校验可见性；bootstrap 与普通分页共用
            # include 查询，不再为了寻找最后一条可见消息补读整轮正文。
            page = self._indexed_page(
                session_id,
                [span],
                include=configured.initial_include,
                next_cursor=None,
                has_more=False,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
            )
            cursor = self._encode_cursor(
                session_id=session_id,
                thread_id=snapshot.thread_id,
                checkpoint_ns=snapshot.checkpoint_ns,
                rollout_id=rollout_id,
                projection_epoch=projection_epoch,
                view_id=indexed.view_id,
                history_view_revision=bounds.history_view_revision,
                turn_read_upper_bound=bounds.upper_turn_ordinal,
                logical_item_read_upper_bound=(
                    bounds.upper_logical_item_ordinal
                ),
                # 游标沿用 snapshot SQL 返回的 1-based logical ordinal。
                turn_ordinal=span.ordinal,
                logical_item_ordinal=None,
                direction="before",
                query_mode="tail",
                effective_include=tuple(sorted(set(configured.initial_include))),
                anchor_turn_id=None,
                window_before_turns=None,
                window_after_turns=None,
                stage=0,
            )
            return page.summaries[0], cursor, projection_epoch
        finally:
            indexed.snapshot.close()

    def load(
        self,
        session_id: str,
        thread_id: str,
        request: TurnHistoryLoadRequest,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> TurnHistoryPageDTO:
        indexed = self._snapshots.read(session_id, thread_id)
        if indexed is None:
            if request.turn_ids is not None and request.cursor is not None:
                raise InvalidTurnCursorError("按 turn_ids 加载详情不能使用历史分页游标")
            if request.cursor is not None:
                raise InvalidTurnCursorError("尚未物化的历史不能使用分页游标")
            binding = self._cursor_binding(
                request, policy or default_history_loading_config(), None,
                history_view_revision=0, turn_read_upper_bound=None,
                logical_item_read_upper_bound=None,
            )
            references = request.turn_ids or (
                [binding.anchor_turn_id] if binding.anchor_turn_id is not None else []
            )
            if references:
                raise StaleTurnReferenceError(session_id=session_id, turn_ids=references)
            return TurnHistoryPageDTO(items=[], next_cursor=None, has_more=False, projection_epoch=1)
        try:
            return self._load_indexed_history(
                session_id,
                request,
                indexed,
                policy=policy,
            )
        finally:
            indexed.snapshot.close()

    def _load_indexed_span(
        self,
        session_id: str,
        span: IndexedTurnSpan,
        *,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        records: list[dict[str, object]],
        projection: dict[str, object] | None = None,
        load_tool_payload: bool = False,
        include: tuple[str, ...] | None = None,
        tool_call_ids: frozenset[str] | None = None,
    ) -> TurnDetailDTO:
        if projection is None:
            raise RuntimeError(f"历史 Turn 缺少 canonical projection: {span.turn_id}")
        job_id = strict_text(
            projection.get("job_id"), field=f"turn_projection.job_id:{span.turn_id}"
        )
        selected_records = records
        messages: list[BaseMessage] = []
        message_sequences: list[int] = []
        fields = set(include or ())
        final_sequence = (
            projection.get("final_message_sequence") if projection is not None else None
        )
        selective = projection is not None and include is not None
        for record in selected_records:
            serialized_message = record.get("message")
            message_type = self._serialized_message_type(record)
            is_tool = message_type == "tool"
            if selective and not self._should_materialize_message(
                record,
                message_type=message_type,
                final_sequence=final_sequence,
                fields=fields,
                load_tool_payload=load_tool_payload,
            ):
                continue
            messages.append(
                self._context_reader.decode_message(
                    serialized_message,
                    summary_only=is_tool and not load_tool_payload,
                )
            )
            message_sequence = record.get("_indexed_sequence")
            if not isinstance(message_sequence, int) or isinstance(
                message_sequence, bool
            ):
                raise TypeError("rollout message 缺少有效 message_sequence")
            message_sequences.append(message_sequence)
        projection_mode = (
            "detail"
            if fields
            & {
                "text",
                "reasoning_detail",
                "tool_call",
                "tool_result",
                "assistant",
                "assistant_text",
                "thinking",
            }
            else "summary"
        )
        response_parts = response_parts_from_records(
            selected_records,
            projection=projection,
            mode=projection_mode,
            include=frozenset(fields),
            tool_call_ids=tool_call_ids,
        )
        return build_detail(
            session_id,
            span.ordinal,
            messages,
            turn_id=span.turn_id,
            thread_id=snapshot.thread_id,
            job_id=job_id,
            root_logical_item_ordinal=span.root_logical_item_ordinal,
            source_view_id=self._context_reader.context_view_id(chain),
            source_view_revision=snapshot.manifest.history_view_revision,
            message_sequences=message_sequences,
            projection=projection,
            response_parts=response_parts,
            tool_call_ids=tool_call_ids,
            include_final=bool(fields & {"final_response", "assistant"}),
        )

    @staticmethod
    def _serialized_message_type(record: Mapping[str, object]) -> str | None:
        message = record.get("message")
        if isinstance(message, Mapping):
            value = message.get("type")
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _serialized_message_has_tool_calls(record: Mapping[str, object]) -> bool:
        message = record.get("message")
        if isinstance(message, Mapping):
            data = message.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("tool_calls"), list):
                return bool(data["tool_calls"])
        return False

    @classmethod
    def _should_materialize_message(
        cls,
        record: Mapping[str, object],
        *,
        message_type: str | None,
        final_sequence: object,
        fields: set[str],
        load_tool_payload: bool,
    ) -> bool:
        sequence = record.get("_indexed_sequence")
        is_final = (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and isinstance(final_sequence, int)
            and not isinstance(final_sequence, bool)
            and sequence == final_sequence
        )
        final_pointer_available = isinstance(final_sequence, int) and not isinstance(
            final_sequence, bool
        )
        if message_type == "human":
            return "user" in fields or "internal" in fields
        if message_type == "tool":
            return load_tool_payload
        if message_type == "ai":
            return (
                not final_pointer_available
                or is_final
                or "assistant" in fields
                or "assistant_text" in fields
                or "text" in fields
                or "reasoning_detail" in fields
                or (
                    load_tool_payload and cls._serialized_message_has_tool_calls(record)
                )
            )
        return "internal" in fields

    def _indexed_page(
        self,
        session_id: str,
        spans: list[IndexedTurnSpan],
        *,
        include: tuple[str, ...],
        next_cursor: str | None,
        has_more: bool,
        before_cursor: str | None = None,
        after_cursor: str | None = None,
        has_before: bool = False,
        has_after: bool = False,
        projection_epoch: int,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        tool_call_ids: tuple[str, ...] = (),
        internal_entries: tuple[InternalExecutionHistoryEntry, ...] = (),
    ) -> TurnHistoryPageDTO:
        budget = DetailReadBudget(LoadLimits())
        summary_budget = DetailReadBudget(LoadLimits())
        load_tool_payload = bool(set(include) & {"tool_call", "tool_result"})
        selected_tool_call_ids = frozenset(tool_call_ids) or None
        projections = self._context_reader.read_turn_projections(
            snapshot,
            chain,
            [span.turn_id for span in spans],
        )
        record_roles = {"user"} if set(include) & {"user", "internal"} else set()
        if set(include) & {
            "assistant",
            "assistant_text",
            "text",
            "reasoning_detail",
        }:
            record_roles.add("assistant")
        tool_kinds: set[str] = set()
        if "tool_call" in include:
            tool_kinds.add("tool_call")
            record_roles.add("assistant")
        if "tool_result" in include:
            tool_kinds.add("tool_result")
        required_sequences: dict[str, set[int]] = {}
        for span in spans:
            projection = projections.get(span.turn_id)
            if projection is None:
                raise RuntimeError(
                    f"canonical Turn 缺少 history projection: {span.turn_id}"
                )
            final_sequence = projection.get("final_message_sequence")
            if (
                isinstance(final_sequence, int)
                and not isinstance(final_sequence, bool)
                and "assistant" in include
            ):
                required_sequences.setdefault(span.turn_id, set()).add(final_sequence)
        if selected_tool_call_ids is not None:
            # 定点补载不读取该 Turn 的其它 assistant/tool message；同一
            # assistant message 内未命中的 call 由 mapper 再次过滤。
            record_roles.discard("assistant")
        records_by_turn = self._context_reader.read_projection_records_batch(
            snapshot,
            turn_ids=[span.turn_id for span in spans],
            chain=chain,
            message_roles=record_roles,
            tool_kinds=tool_kinds,
            tool_call_ids=selected_tool_call_ids,
            required_sequences=required_sequences,
        )
        items: list[TurnDetailDTO] = []
        summaries: list[TurnSummaryDTO] = []
        for span in spans:
            detail = self._load_indexed_span(
                session_id,
                span,
                snapshot=snapshot,
                chain=chain,
                records=records_by_turn.get(span.turn_id, []),
                projection=projections.get(span.turn_id),
                load_tool_payload=load_tool_payload,
                include=include,
                tool_call_ids=selected_tool_call_ids,
            )
            summaries.append(
                summary(
                    project_detail(
                        detail,
                        DEFAULT_INITIAL_INCLUDE,
                        summary_budget,
                    )
                )
            )
            items.append(project_detail(detail, include, budget))
        return TurnHistoryPageDTO(
            items=items,
            source_view_id=self._context_reader.context_view_id(chain),
            source_view_revision=snapshot.manifest.history_view_revision,
            summaries=summaries,
            internal_executions=self._project_internal_entries(
                internal_entries,
                include=include,
            ),
            next_cursor=next_cursor,
            has_more=has_more,
            before_cursor=before_cursor,
            after_cursor=after_cursor,
            has_before=has_before,
            has_after=has_after,
            projection_epoch=projection_epoch,
        )

    @staticmethod
    def _project_internal_entries(
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
                    execution_id=entry.execution_id,
                    job_id=entry.job_id,
                    session_id=entry.session_id,
                    thread_id=entry.thread_id,
                    outcome=outcome.value,
                    created_at=entry.created_at,
                    updated_at=entry.updated_at,
                )
                projections[entry.execution_id] = projection
            elif (
                projection.job_id != entry.job_id
                or projection.session_id != entry.session_id
                or projection.thread_id != entry.thread_id
                or projection.outcome != outcome.value
                or projection.created_at != entry.created_at
                or projection.updated_at != entry.updated_at
            ):
                raise RuntimeError(
                    "同一 internal execution 的 durable metadata 不一致: "
                    f"{entry.execution_id}"
                )

            item = entry.item
            if "internal" in fields and item.semantic_kind == SemanticKind.RUNTIME_NOTICE:
                if (
                    item.payload_kind != PayloadKind.STRUCTURED_CONTENT
                    or not isinstance(item.payload, Mapping)
                    or not set(item.payload) & {"prompt", "display_part"}
                ):
                    continue
                notice = RuntimeNoticePayload.from_dict(item.payload)
                display_part = notice.display_part
                if display_part is not None and display_part.kind == "generated_session_result":
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
                execution_id=projection.execution_id,
                job_id=projection.job_id,
                session_id=projection.session_id,
                thread_id=projection.thread_id,
                status=projection.outcome,
                created_at=projection.created_at,
                updated_at=projection.updated_at,
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
