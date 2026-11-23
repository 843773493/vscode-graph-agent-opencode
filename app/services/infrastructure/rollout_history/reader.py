"""按已提交 snapshot 查询 rollout 历史，展示转换由 mapping 层负责。"""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic

from langchain_core.messages import BaseMessage

from app.core.exceptions import NotFoundError
from app.core.history_loading import (
    DEFAULT_INITIAL_INCLUDE,
    HistoryLoadingConfig,
    default_history_loading_config,
)
from app.schemas.internal_v2.turn import (
    ContextNoticeAnchorDTO,
    ContextNoticeBatchDTO,
    TurnDetailDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnSummaryDTO,
)
from app.schemas.internal_v2.turn_search import (
    ThreadItemInspectionDTO,
    ThreadItemInspectionRequest,
    ThreadItemLocationDTO,
    ThreadToolResultListDTO,
    TurnMessageSearchRequest,
    TurnMessageSearchResultsDTO,
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
from app.services.infrastructure.rollout_history.context_notices import (
    decode_context_cursor,
    read_notice_parts,
)
from app.services.infrastructure.rollout_history.errors import (
    HistoryItemUnavailableError,
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)
from app.services.infrastructure.rollout_history.message_search import (
    MessageSearchBudget,
    search_messages,
    verify_message_reference,
)
from app.services.infrastructure.rollout_history.paging import (
    HistoryPageReadMixin,
)
from app.services.infrastructure.rollout_history.snapshot import (
    IndexedHistorySnapshots,
    IndexedTurnSpan,
)
from app.services.infrastructure.rollout_history.tool_result import (
    read_tool_result_output,
)
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.mapping.itemized.history_turns.detail import build_detail
from app.services.mapping.itemized.history_turns.internal_execution import (
    build_internal_execution_history,
)
from app.services.mapping.itemized.history_turns.item_content import item_contents
from app.services.mapping.itemized.history_turns.projection import (
    HistoryProjectionBudget,
    project_detail,
    summary,
)
from app.services.mapping.itemized.history_turns.tools import tool_result_metadata_dto
from app.services.mapping.turn_response_parts import (
    enrich_selected_item_parts,
    response_parts_from_records,
)

_ASSISTANT_HISTORY_FIELDS = frozenset(
    {"assistant", "assistant_text", "text", "reasoning_detail"}
)


class RolloutHistoryReader(HistoryPageReadMixin):
    def __init__(self, context_reader: RolloutContextReader, *, tool_outputs: ToolOutputStore | None = None) -> None:
        self._context_reader = context_reader
        self._tool_outputs = tool_outputs
        self._snapshots = IndexedHistorySnapshots(context_reader)

    def search_messages(
        self, session_id: str, thread_id: str, request: TurnMessageSearchRequest,
        *, budget: MessageSearchBudget | None = None,
    ) -> TurnMessageSearchResultsDTO:
        indexed = self._snapshots.read(session_id, thread_id)
        if indexed is None:
            if request.cursor is not None:
                raise InvalidTurnCursorError("尚未物化的历史不能使用搜索 cursor")
            return TurnMessageSearchResultsDTO(
                session_id=session_id, thread_id=thread_id, items=[], cursor=None,
                complete=True, limited_reason=None, scanned_items=0, scanned_bytes=0,
            )
        try:
            return search_messages(self._context_reader, indexed, request, budget=budget, tool_outputs=self._tool_outputs)
        finally:
            indexed.snapshot.close()

    def inspect_item(self, session_id: str, thread_id: str, request: ThreadItemInspectionRequest) -> ThreadItemInspectionDTO:
        indexed = self._snapshots.read(session_id, thread_id)
        if indexed is None:
            raise NotFoundError("当前 Thread 尚无已提交 Item")
        try:
            row = self._context_reader.inspect_item_metadata(indexed.snapshot, indexed.chain,
                item_id=request.item_id, message_id=request.message_id) if indexed.view_id else None
            if row is None:
                raise NotFoundError("Item 不存在或已不在当前 Thread 的可见历史中")
            location = ThreadItemLocationDTO(**dict(zip(("item_id", "turn_id", "execution_id", "job_id",
                "model_call_id", "message_id", "semantic_kind", "content_hash", "byte_length"), row, strict=True)))
            result = ThreadItemInspectionDTO(location=location)
            if request.operation in {"content_metadata", "content"}:
                if location.byte_length > 8 * 1024 * 1024:
                    raise ValueError("单个 Item 超过 8 MiB 内容读取上限，请打开其原始资源")
                item, = self._context_reader.read_canonical_items(indexed.snapshot, item_ids=[location.item_id])
                output = read_tool_result_output(item, self._tool_outputs, 8 * 1024 * 1024 - location.byte_length) if item.semantic_kind == "tool_result" else None
                if output is not None and output[0] is None:
                    raise ValueError("此工具结果超过 8 MiB 内容读取上限，请打开其原始输出资源")
                result.contents = item_contents(item, tool_output=output[0] if output is not None else None)
                if request.operation == "content_metadata":
                    # 格式沿同一 canonical 投影推导；选择打开方式不向客户端预传正文。
                    result.contents = [content.model_copy(update={"content": None}) for content in result.contents]
            return result
        finally:
            indexed.snapshot.close()

    def list_tool_results(self, session_id: str, thread_id: str, *, after: int = 0,
                          limit: int = 100, view_id: str | None = None,
                          view_revision: int | None = None) -> ThreadToolResultListDTO:
        indexed = self._snapshots.read(session_id, thread_id)
        if indexed is None or indexed.view_id is None:
            if indexed is not None:
                indexed.snapshot.close()
            if view_id is not None:
                raise InvalidTurnCursorError("当前 Thread 没有可续查的历史视图")
            return ThreadToolResultListDTO(session_id=session_id, thread_id=thread_id, items=[],
                source_view_id=None, source_view_revision=0)
        try:
            if view_id is not None and (indexed.view_id != view_id or
                    indexed.snapshot.manifest.history_view_revision != view_revision):
                raise StaleTurnCursorError(session_id=session_id,
                    cursor_epoch=indexed.projection_epoch, current_epoch=indexed.projection_epoch,
                    cursor_history_view_revision=view_revision,
                    current_history_view_revision=indexed.snapshot.manifest.history_view_revision)
            rows = self._context_reader.read_tool_result_metadata(indexed.snapshot, indexed.chain,
                after=after, limit=limit + 1, deadline=monotonic() + 1.0)
            return ThreadToolResultListDTO(session_id=session_id, thread_id=thread_id,
                items=[tool_result_metadata_dto(row) for row in rows[:limit]],
                next_after=rows[limit - 1][0] if len(rows) > limit else None,
                source_view_id=indexed.view_id, source_view_revision=indexed.snapshot.manifest.history_view_revision)
        finally:
            indexed.snapshot.close()

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
                logical_item_read_upper_bound=(bounds.upper_logical_item_ordinal),
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
            if request.anchor_item_id is not None or request.item_ids is not None:
                raise HistoryItemUnavailableError(session_id=session_id, thread_id=thread_id,
                    item_id=request.anchor_item_id or request.item_ids[0])
            if request.context_cursor is not None:
                raise InvalidTurnCursorError("尚未物化的历史不能续读上下文元数据")
            if request.turn_ids is not None and request.cursor is not None:
                raise InvalidTurnCursorError("按 turn_ids 加载详情不能使用历史分页游标")
            if request.cursor is not None:
                raise InvalidTurnCursorError("尚未物化的历史不能使用分页游标")
            binding = self._cursor_binding(
                request,
                policy or default_history_loading_config(),
                None,
                history_view_revision=0,
                turn_read_upper_bound=None,
                logical_item_read_upper_bound=None,
            )
            references = request.turn_ids or (
                [binding.anchor_turn_id] if binding.anchor_turn_id is not None else []
            )
            if references:
                raise StaleTurnReferenceError(
                    session_id=session_id, turn_ids=references
                )
            return TurnHistoryPageDTO(
                items=[], next_cursor=None, has_more=False, projection_epoch=1
            )
        try:
            if request.source_view_id is not None:
                if request.source_view_id != indexed.view_id:
                    raise InvalidTurnCursorError("详情来源不属于当前 context view")
                if request.source_view_revision != indexed.snapshot.manifest.history_view_revision:
                    raise StaleTurnCursorError(session_id=session_id, cursor_epoch=indexed.projection_epoch,
                        current_epoch=indexed.projection_epoch, cursor_history_view_revision=request.source_view_revision,
                        current_history_view_revision=indexed.snapshot.manifest.history_view_revision)
            located_item = None
            for item_id in ([request.anchor_item_id] if request.anchor_item_id else request.item_ids or []):
                row = self._context_reader.inspect_item_metadata(indexed.snapshot, indexed.chain,
                    item_id=item_id, message_id=None) if indexed.view_id else None
                if row is None:
                    raise HistoryItemUnavailableError(session_id=session_id, thread_id=thread_id, item_id=item_id)
                location = ThreadItemLocationDTO(**dict(zip(("item_id", "turn_id", "execution_id", "job_id",
                    "model_call_id", "message_id", "semantic_kind", "content_hash", "byte_length"), row, strict=True)))
                if request.item_ids is not None and (location.turn_id != (request.turn_ids[0] if request.turn_ids else None)
                    or request.execution_id is not None and location.execution_id != request.execution_id):
                    raise InvalidTurnCursorError("所选 Item 不属于请求过程")
                if request.anchor_item_id:
                    located_item = location
            if request.context_cursor is not None:
                cursor = decode_context_cursor(request.context_cursor, indexed.snapshot, indexed.view_id)
                notices = read_notice_parts(self._context_reader, indexed.snapshot, indexed.chain,
                    turn_ids=cursor.turn_ids, execution_ids=cursor.execution_ids, cursor=cursor)
                return TurnHistoryPageDTO(items=[], projection_epoch=indexed.projection_epoch,
                    source_view_id=indexed.view_id,
                    source_view_revision=indexed.snapshot.manifest.history_view_revision,
                    context_notices=[ContextNoticeBatchDTO(owner_id=owner,
                        owner_kind="turn" if owner in cursor.turn_ids else "execution", parts=parts)
                        for owner, parts in notices.items()])
            located = (
                verify_message_reference(self._context_reader, indexed, request.anchor_turn_id, request.anchor_message,
                                         tool_outputs=self._tool_outputs)
                if request.anchor_message is not None else None
            )
            located_tool = None
            if request.anchor_tool_result is not None:
                rows = self._context_reader.read_tool_result_metadata(indexed.snapshot, indexed.chain,
                    after=0, limit=1, deadline=monotonic() + 1.0, item_id=request.anchor_tool_result)
                if not rows or rows[0][2] != request.anchor_turn_id:
                    raise StaleTurnReferenceError(session_id=session_id, turn_ids=[request.anchor_turn_id])
                located_tool = tool_result_metadata_dto(rows[0])
            page = self._load_indexed_history(
                session_id,
                request,
                indexed,
                policy=policy,
                located_tool_call_id=located.tool_call_id if located is not None else located_tool.tool_call_id if located_tool else None,
            )
            return page.model_copy(update={"located_message": located, "located_tool_result": located_tool, "located_item": located_item})
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
        activity_before: tuple[int, int] | None = None, activity_limit: int | None = None,
        item_ids: tuple[str, ...] | None = None,
        projections: dict[str, dict[str, object]] | None = None,
        anchor_context: ContextNoticeAnchorDTO | None = None,
        context_turn_id: str | None = None,
    ) -> TurnHistoryPageDTO:
        budget = HistoryProjectionBudget()
        summary_budget = HistoryProjectionBudget()
        include_fields = set(include)
        load_tool_payload = (
            "tool_call" in include_fields or "tool_result" in include_fields
        )
        selected_tool_call_ids = frozenset(tool_call_ids) or None
        projections = projections if projections is not None else self._context_reader.read_turn_projections(
            snapshot,
            chain,
            [span.turn_id for span in spans], activity_before=activity_before,
            activity_limit=activity_limit, item_ids=item_ids, outline="activity_outline" in include, include=frozenset(include),
        )
        record_roles = (
            {"user"}
            if "user" in include_fields or "internal" in include_fields
            else set()
        )
        if "activity_outline" not in include_fields and not include_fields.isdisjoint(_ASSISTANT_HISTORY_FIELDS):
            record_roles.add("assistant")
        tool_kinds: set[str] = set()
        if "tool_call" in include_fields:
            tool_kinds.add("tool_call")
            record_roles.add("assistant")
        if "tool_result" in include_fields:
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
                and "assistant" in include_fields
            ):
                required_sequences.setdefault(span.turn_id, set()).add(final_sequence)
        if selected_tool_call_ids is not None:
            # 定点补载不读取该 Turn 的其它 assistant/tool message；同一
            # assistant message 内未命中的 call 由 mapper 再次过滤。
            record_roles.discard("assistant")
        if item_ids is not None:
            record_roles.clear()
            tool_kinds.clear()
            required_sequences.clear()
        records_by_turn = self._context_reader.read_projection_records_batch(
            snapshot,
            turn_ids=[span.turn_id for span in spans],
            chain=chain,
            message_roles=record_roles,
            tool_kinds=tool_kinds,
            tool_call_ids=selected_tool_call_ids,
            required_sequences=required_sequences,
        )
        if item_ids is not None:
            records_by_turn = {}
        items: list[TurnDetailDTO] = []
        summaries: list[TurnSummaryDTO] = []
        notices = read_notice_parts(self._context_reader, snapshot, chain,
            turn_ids=[context_turn_id] if context_turn_id else [span.turn_id for span in spans],
            execution_ids=[] if context_turn_id else list(dict.fromkeys(entry.execution_id for entry in internal_entries)),
            anchor_context=anchor_context)
        # 先为窗口末端分配正文预算，避免旧回复耗尽预算后截空最新回复。
        # DTO 仍按请求的原顺序返回。
        for span in reversed(spans):
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
            if projections[span.turn_id].get("activity_has_more") is True:
                detail.detail_truncated = True
            if item_ids is not None:
                canonical = self._context_reader.read_canonical_items(snapshot, item_ids=list(item_ids))
                detail.response_parts = enrich_selected_item_parts(detail.response_parts, canonical, include=frozenset(include))
            detail.response_parts.extend(notices.get(span.turn_id, ()))
            if activity_limit is None and item_ids is None:
                summaries.append(summary(
                    project_detail(
                        detail,
                        DEFAULT_INITIAL_INCLUDE,
                        summary_budget,
                    )))
            items.append(project_detail(detail, include, budget))
        items.reverse()
        summaries.reverse()
        internal_executions = build_internal_execution_history(internal_entries, include=include, budget=budget)
        for execution in internal_executions:
            execution.response_parts.extend(notices.get(execution.execution_id, ()))
        return TurnHistoryPageDTO(
            items=items,
            source_view_id=self._context_reader.context_view_id(chain),
            source_view_revision=snapshot.manifest.history_view_revision,
            summaries=summaries,
            internal_executions=internal_executions,
            next_cursor=next_cursor,
            has_more=has_more,
            before_cursor=before_cursor,
            after_cursor=after_cursor,
            has_before=has_before,
            has_after=has_after,
            projection_epoch=projection_epoch,
        )
