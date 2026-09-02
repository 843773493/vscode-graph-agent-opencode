"""直接从 rollout JSONL 和 SQLite 索引组装 Web 会话历史。"""

from __future__ import annotations

import base64
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agents.providers.litellm_content import reasoning_projection_rows
from app.agents.providers.litellm_content import (
    visible_text as litellm_visible_text,
)
from app.core.history_loading import (
    HistoryLoadingConfig,
    default_history_loading_config,
)
from app.core.rollout_context_reader import ContextChain, RolloutContextReader
from app.core.rollout_storage import RolloutReadSnapshot
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.turn import (
    TurnActivityStatsDTO,
    TurnAttachmentDTO,
    TurnCursorDTO,
    TurnDetailDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnResponsePartDTO,
    TurnSummaryDTO,
    TurnThinkingBlockDTO,
    TurnToolSummaryDTO,
    TurnUserMessageDTO,
    TurnUserMessageSummaryDTO,
)
from app.services.mapping.turn_response_parts import response_parts_from_records
from app.services.mapping.user_message_content_projection import user_content_projection

from .turn_history.load_plan import DetailReadBudget, LoadLimits
from .turn_history.models import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)


@dataclass(slots=True)
class _HistoryTurn:
    detail: TurnDetailDTO
    messages: list[BaseMessage]


@dataclass(frozen=True, slots=True)
class _IndexedTurnSpan:
    turn_id: str
    first_sequence: int
    last_sequence: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class _IndexedHistoryCacheEntry:
    rollout_id: str
    projection_epoch: int
    committed_sequence: int
    active_branch_id: str
    checkpoint_id: str
    message_sequence: int
    view_id: str
    turn_count: int
    context_ranges: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class _IndexedHistory:
    rollout_id: str
    projection_epoch: int
    view_id: str | None
    turn_count: int
    snapshot: RolloutReadSnapshot
    chain: ContextChain


_INDEXED_HISTORY_CACHE_LIMIT = 64
_TOOL_SUMMARY_LIMIT = 64


class RolloutHistoryReader:
    """rollout 是历史权威来源，Trace/TurnHistory 不参与读取。"""

    def __init__(self, context_reader: RolloutContextReader) -> None:
        self._context_reader = context_reader
        self._indexed_history_cache: OrderedDict[str, _IndexedHistoryCacheEntry] = (
            OrderedDict()
        )
        self._indexed_history_cache_lock = threading.RLock()

    def bootstrap(
        self,
        session_id: str,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> tuple[TurnSummaryDTO | None, str | None, int]:
        indexed = self._read_indexed_history(session_id)
        if indexed is not None:
            try:
                rollout_id = indexed.rollout_id
                projection_epoch = indexed.projection_epoch
                snapshot = indexed.snapshot
                if indexed.view_id is None or indexed.turn_count == 0:
                    return None, None, projection_epoch
                configured = policy or default_history_loading_config()
                raw_spans, _ = self._context_reader.read_context_turn_page(
                    snapshot,
                    indexed.chain,
                    direction="tail",
                    anchor_ordinal=None,
                    # 中断或控制操作可能在尾部留下只含 internal 消息的 Turn；
                    # 一次多取少量候选，避免把不可见控制记录当成最新会话内容。
                    limit=max(8, configured.initial_turns),
                )
                latest: _HistoryTurn | None = None
                span: _IndexedTurnSpan | None = None
                for raw_span in reversed(raw_spans):
                    candidate_span = self._span_from_row(raw_span)
                    projections = self._context_reader.read_turn_projections(
                        snapshot, [candidate_span.turn_id]
                    )
                    candidate = self._load_indexed_span(
                        session_id,
                        candidate_span,
                        snapshot=snapshot,
                        chain=indexed.chain,
                        projection=projections.get(candidate_span.turn_id),
                        include=configured.initial_include,
                    )
                    if self._has_public_content(candidate.detail):
                        latest = candidate
                        span = candidate_span
                        break
                if latest is None or span is None:
                    return None, None, projection_epoch
                cursor = self._encode_cursor(
                    session_id=session_id,
                    rollout_id=rollout_id,
                    projection_epoch=projection_epoch,
                    anchor_ordinal=latest.detail.ordinal,
                    direction="before",
                    stage=0,
                )
                return self._summary(latest.detail), cursor, projection_epoch
            finally:
                indexed.snapshot.close()

        raise RuntimeError("rollout 历史索引不可用")

    def load(
        self,
        session_id: str,
        request: TurnHistoryLoadRequest,
        *,
        policy: HistoryLoadingConfig | None = None,
    ) -> TurnHistoryPageDTO:
        indexed = self._read_indexed_history(session_id)
        if indexed is not None:
            try:
                return self._load_indexed_history(
                    session_id,
                    request,
                    indexed,
                    policy=policy,
                )
            finally:
                indexed.snapshot.close()

        raise RuntimeError("rollout 历史索引不可用")

    def _read_indexed_history(
        self,
        session_id: str,
    ) -> _IndexedHistory | None:
        # 历史读取必须保持纯只读：open_snapshot() 持有 rollout 的共享文件锁，
        # 而 repair_active_context_view() 会申请同一个文件的独占写锁。若调用方
        # 已经持有读快照（例如历史分页或异常回收路径），先 repair 会在 Linux
        # 的 flock 上自锁。索引修复属于显式维护/写入边界，不能塞进读取入口；
        # 这里只读取一个固定快照，异常也由下面的 finally 路径关闭它。
        snapshot = self._context_reader.open_snapshot(session_id)
        try:
            return self._read_indexed_history_snapshot(session_id, snapshot)
        except Exception:
            snapshot.close()
            raise

    def _read_indexed_history_snapshot(
        self,
        session_id: str,
        snapshot: RolloutReadSnapshot,
    ) -> _IndexedHistory | None:
        """只通过统一 reader 解析逻辑链和 SQLite Turn 范围。"""
        manifest = snapshot.manifest
        checkpoint = self._context_reader.latest_checkpoint(snapshot)
        if checkpoint is None:
            return _IndexedHistory(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                view_id=None,
                turn_count=0,
                snapshot=snapshot,
                chain=ContextChain(message_sequence=0, ranges=()),
            )
        cache_entry = self._cached_indexed_history(
            session_id,
            rollout_id=manifest.rollout_id,
            projection_epoch=manifest.projection_epoch,
            committed_sequence=manifest.committed_sequence,
            active_branch_id=manifest.active_branch_id,
            checkpoint_id=checkpoint.checkpoint_id,
            message_sequence=checkpoint.message_sequence,
        )
        if cache_entry is not None:
            return _IndexedHistory(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                view_id=cache_entry.view_id,
                turn_count=cache_entry.turn_count,
                snapshot=snapshot,
                chain=ContextChain(
                    message_sequence=checkpoint.message_sequence,
                    ranges=cache_entry.context_ranges,
                ),
            )
        chain = self._context_reader.resolve_chain(
            snapshot,
            checkpoint.message_sequence,
        )
        view_id = self._context_reader.context_view_id(chain)
        turn_count = self._context_reader.context_turn_count(snapshot, chain)
        self._cache_indexed_history(
            session_id,
            _IndexedHistoryCacheEntry(
                rollout_id=manifest.rollout_id,
                projection_epoch=manifest.projection_epoch,
                committed_sequence=manifest.committed_sequence,
                active_branch_id=manifest.active_branch_id,
                checkpoint_id=checkpoint.checkpoint_id,
                message_sequence=checkpoint.message_sequence,
                view_id=view_id,
                turn_count=turn_count,
                context_ranges=chain.ranges,
            ),
        )
        return _IndexedHistory(
            rollout_id=manifest.rollout_id,
            projection_epoch=manifest.projection_epoch,
            view_id=view_id,
            turn_count=turn_count,
            snapshot=snapshot,
            chain=chain,
        )

    def _cached_indexed_history(
        self,
        session_id: str,
        *,
        rollout_id: str,
        projection_epoch: int,
        committed_sequence: int,
        active_branch_id: str,
        checkpoint_id: str,
        message_sequence: int,
    ) -> _IndexedHistoryCacheEntry | None:
        with self._indexed_history_cache_lock:
            entry = self._indexed_history_cache.get(session_id)
            if entry is None:
                return None
            if (
                entry.rollout_id != rollout_id
                or entry.projection_epoch != projection_epoch
                or entry.committed_sequence != committed_sequence
                or entry.active_branch_id != active_branch_id
                or entry.checkpoint_id != checkpoint_id
                or entry.message_sequence != message_sequence
            ):
                self._indexed_history_cache.pop(session_id, None)
                return None
            self._indexed_history_cache.move_to_end(session_id)
            return entry

    def _cache_indexed_history(
        self,
        session_id: str,
        entry: _IndexedHistoryCacheEntry,
    ) -> None:
        with self._indexed_history_cache_lock:
            self._indexed_history_cache[session_id] = entry
            self._indexed_history_cache.move_to_end(session_id)
            while len(self._indexed_history_cache) > _INDEXED_HISTORY_CACHE_LIMIT:
                self._indexed_history_cache.popitem(last=False)

    def _discard_cached_indexed_history(self, session_id: str) -> None:
        with self._indexed_history_cache_lock:
            self._indexed_history_cache.pop(session_id, None)

    def _load_indexed_history(
        self,
        session_id: str,
        request: TurnHistoryLoadRequest,
        indexed: _IndexedHistory,
        *,
        policy: HistoryLoadingConfig | None,
    ) -> TurnHistoryPageDTO:
        rollout_id = indexed.rollout_id
        projection_epoch = indexed.projection_epoch
        snapshot = indexed.snapshot
        decoded_cursor = self._decode_cursor(
            session_id,
            rollout_id,
            projection_epoch,
            request.cursor,
        )
        if indexed.view_id is None:
            return TurnHistoryPageDTO(
                items=[],
                next_cursor=None,
                has_more=False,
                before_cursor=None,
                after_cursor=None,
                projection_epoch=projection_epoch,
            )
        configured = policy or default_history_loading_config()
        include = tuple(request.include or configured.initial_include)

        if request.turn_ids is not None:
            rows = self._context_reader.read_context_turn_ids(
                snapshot,
                indexed.chain,
                request.turn_ids,
            )
            by_turn_id = {row[0]: self._span_from_row(row) for row in rows}
            if len(by_turn_id) != len(set(request.turn_ids)):
                missing = [
                    turn_id for turn_id in request.turn_ids if turn_id not in by_turn_id
                ]
                canonical_projections = self._context_reader.read_turn_projections(
                    snapshot,
                    missing,
                )
                stale = [
                    turn_id for turn_id in missing if turn_id in canonical_projections
                ]
                if stale:
                    raise StaleTurnReferenceError(
                        session_id=session_id,
                        turn_ids=stale,
                    )
                raise KeyError(f"rollout Turn 不存在: {missing}")
            selected = [by_turn_id[turn_id] for turn_id in request.turn_ids]
            return self._indexed_page(
                session_id,
                selected,
                include=include,
                tool_call_ids=tuple(request.tool_call_ids or ()),
                next_cursor=None,
                has_more=False,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
            )

        stage = decoded_cursor.stage if decoded_cursor is not None else 0
        if request.direction == "tail" and request.cursor is None:
            limit = self._bounded_limit(request.turns, configured.initial_turns)
            raw_spans, has_more = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="tail",
                anchor_ordinal=None,
                limit=limit,
            )
            selected = [self._span_from_row(row) for row in raw_spans]
            next_cursor = self._indexed_before_cursor(
                session_id,
                rollout_id,
                projection_epoch,
                selected,
                stage=0,
            )
            return self._indexed_page(
                session_id,
                selected,
                include=include,
                next_cursor=next_cursor,
                has_more=has_more,
                before_cursor=next_cursor,
                has_before=has_more,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
            )

        default_limit = (
            configured.anchor_after_turns
            if request.direction == "after"
            else configured.anchor_before_turns
        )
        limit = self._bounded_limit(request.turns, default_limit)
        if request.direction in {"before", "older"}:
            anchor = self._anchor_ordinal(decoded_cursor, indexed.turn_count + 1)
            raw_spans, has_more = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="before",
                anchor_ordinal=anchor,
                limit=limit,
            )
            selected = [self._span_from_row(row) for row in raw_spans]
            next_cursor = self._indexed_before_cursor(
                session_id,
                rollout_id,
                projection_epoch,
                selected,
                stage=stage + 1,
            )
            return self._indexed_page(
                session_id,
                selected,
                include=tuple(request.include or configured.anchor_include),
                next_cursor=next_cursor,
                has_more=has_more,
                before_cursor=next_cursor,
                has_before=has_more,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
            )

        if request.direction == "head":
            raw_spans, has_more = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="head",
                anchor_ordinal=None,
                limit=limit,
            )
            selected = [self._span_from_row(row) for row in raw_spans]
            next_cursor = self._indexed_after_cursor(
                session_id,
                rollout_id,
                projection_epoch,
                selected,
                stage=stage + 1,
            )
            return self._indexed_page(
                session_id,
                selected,
                include=tuple(request.include or configured.anchor_include),
                next_cursor=next_cursor,
                has_more=has_more,
                after_cursor=next_cursor,
                has_after=has_more,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
            )

        if request.direction == "after":
            anchor = self._anchor_ordinal(decoded_cursor, 0)
            raw_spans, has_more = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="after",
                anchor_ordinal=anchor,
                limit=limit,
            )
            selected = [self._span_from_row(row) for row in raw_spans]
            next_cursor = self._indexed_after_cursor(
                session_id,
                rollout_id,
                projection_epoch,
                selected,
                stage=stage + 1,
            )
            return self._indexed_page(
                session_id,
                selected,
                include=tuple(request.include or configured.anchor_include),
                next_cursor=next_cursor,
                has_more=has_more,
                after_cursor=next_cursor,
                has_after=has_more,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
            )

        if request.direction == "around":
            if request.anchor_turn_id is not None:
                anchor_rows = self._context_reader.read_context_turn_ids(
                    snapshot,
                    indexed.chain,
                    [request.anchor_turn_id],
                )
                if not anchor_rows:
                    canonical_projections = self._context_reader.read_turn_projections(
                        snapshot,
                        [request.anchor_turn_id],
                    )
                    if request.anchor_turn_id in canonical_projections:
                        raise StaleTurnReferenceError(
                            session_id=session_id,
                            turn_ids=[request.anchor_turn_id],
                        )
                    raise KeyError(f"rollout Turn 不存在: {request.anchor_turn_id}")
                anchor = anchor_rows[0][3]
            else:
                anchor = self._anchor_ordinal(decoded_cursor, 0)
            before = min(
                request.before_turns
                if request.before_turns is not None
                else configured.anchor_before_turns,
                64,
            )
            after = min(
                request.after_turns
                if request.after_turns is not None
                else configured.anchor_after_turns,
                64,
            )
            rows = self._context_reader.read_context_turn_window(
                snapshot,
                indexed.chain,
                anchor_ordinal=max(1, anchor),
                before=before,
                after=after,
            )
            selected = [self._span_from_row(row) for row in rows]
            has_before = bool(selected) and selected[0].ordinal > 1
            has_after = bool(selected) and selected[-1].ordinal < indexed.turn_count
            before_cursor = (
                self._indexed_before_cursor(
                    session_id,
                    rollout_id,
                    projection_epoch,
                    selected,
                    stage=0,
                )
                if has_before
                else None
            )
            after_cursor = (
                self._indexed_after_cursor(
                    session_id,
                    rollout_id,
                    projection_epoch,
                    selected,
                    stage=0,
                )
                if has_after
                else None
            )
            return self._indexed_page(
                session_id,
                selected,
                include=tuple(request.include or configured.anchor_include),
                next_cursor=None,
                has_more=False,
                before_cursor=before_cursor,
                after_cursor=after_cursor,
                has_before=has_before,
                has_after=has_after,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
            )

        raise InvalidTurnCursorError(f"不支持的历史方向: {request.direction}")

    @staticmethod
    def _span_from_row(row: tuple[str, int, int, int]) -> _IndexedTurnSpan:
        return _IndexedTurnSpan(
            turn_id=row[0],
            first_sequence=row[1],
            last_sequence=row[2],
            ordinal=row[3],
        )

    def _load_indexed_span(
        self,
        session_id: str,
        span: _IndexedTurnSpan,
        *,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        records: list[dict[str, object]] | None = None,
        projection: dict[str, object] | None = None,
        load_tool_payload: bool = False,
        include: tuple[str, ...] | None = None,
        tool_call_ids: frozenset[str] | None = None,
    ) -> _HistoryTurn:
        selected_records = records
        if selected_records is None:
            selected_records = self._context_reader.read_projection_records(
                snapshot,
                after_sequence=span.first_sequence - 1,
                through_sequence=span.last_sequence,
                chain=chain,
                turn_id=span.turn_id,
                kinds=("message_append",),
            )
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
            if not isinstance(message_sequence, int) or isinstance(message_sequence, bool):
                raise TypeError("rollout message 缺少有效 message_sequence")
            message_sequences.append(message_sequence)
        if not messages:
            raise RuntimeError(f"rollout Turn 没有可读取的消息: {span.turn_id}")
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
        return _HistoryTurn(
            detail=self._build_detail(
                session_id,
                span.ordinal,
                messages,
                turn_id=span.turn_id,
                message_sequences=message_sequences,
                projection=projection,
                response_parts=response_parts,
                tool_call_ids=tool_call_ids,
            ),
            messages=messages,
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
        spans: list[_IndexedTurnSpan],
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
    ) -> TurnHistoryPageDTO:
        budget = DetailReadBudget(LoadLimits())
        load_tool_payload = bool(set(include) & {"tool_call", "tool_result"})
        selected_tool_call_ids = frozenset(tool_call_ids) or None
        projections = self._context_reader.read_turn_projections(
            snapshot,
            [span.turn_id for span in spans],
        )
        record_roles = {"user"}
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
                record_roles.add("assistant")
                continue
            final_sequence = projection.get("final_message_sequence")
            if (
                isinstance(final_sequence, int)
                and not isinstance(final_sequence, bool)
            ):
                required_sequences.setdefault(span.turn_id, set()).add(final_sequence)
            elif not isinstance(final_sequence, int) or isinstance(
                final_sequence, bool
            ):
                # 没有显式 final pointer 时，读取 AIMessage 供 heuristic fallback。
                record_roles.add("assistant")
        if selected_tool_call_ids is not None:
            # 定点补载不读取该 Turn 的其它 assistant/tool message；同一
            # assistant message 内未命中的 call 由 mapper 再次过滤。
            record_roles = set()
            required_sequences = {}
        records_by_turn = self._context_reader.read_projection_records_batch(
            snapshot,
            turn_ids=[span.turn_id for span in spans],
            chain=chain,
            message_roles=record_roles,
            tool_kinds=tool_kinds,
            tool_call_ids=selected_tool_call_ids,
            required_sequences=required_sequences,
        )
        items = [
            self._project(
                self._load_indexed_span(
                    session_id,
                    span,
                    snapshot=snapshot,
                    chain=chain,
                    records=records_by_turn.get(span.turn_id, []),
                    projection=projections.get(span.turn_id),
                    load_tool_payload=load_tool_payload,
                    include=include,
                    tool_call_ids=selected_tool_call_ids,
                ).detail,
                include,
                budget,
            )
            for span in spans
        ]
        return TurnHistoryPageDTO(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            before_cursor=before_cursor,
            after_cursor=after_cursor,
            has_before=has_before,
            has_after=has_after,
            projection_epoch=projection_epoch,
        )

    def _indexed_before_cursor(
        self,
        session_id: str,
        rollout_id: str,
        projection_epoch: int,
        selected: list[_IndexedTurnSpan],
        *,
        stage: int,
    ) -> str | None:
        if not selected:
            return None
        return self._encode_cursor(
            session_id=session_id,
            rollout_id=rollout_id,
            projection_epoch=projection_epoch,
            anchor_ordinal=selected[0].ordinal,
            direction="before",
            stage=stage,
        )

    def _indexed_after_cursor(
        self,
        session_id: str,
        rollout_id: str,
        projection_epoch: int,
        selected: list[_IndexedTurnSpan],
        *,
        stage: int,
    ) -> str | None:
        if not selected:
            return None
        return self._encode_cursor(
            session_id=session_id,
            rollout_id=rollout_id,
            projection_epoch=projection_epoch,
            anchor_ordinal=selected[-1].ordinal,
            direction="after",
            stage=stage,
        )

    def cursor_stage(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        value = self._decode_cursor_payload(cursor)
        stage = value.get("stage")
        if isinstance(stage, bool) or not isinstance(stage, int) or stage < 0:
            raise InvalidTurnCursorError("历史游标 stage 非法")
        return stage

    def _build_detail(
        self,
        session_id: str,
        ordinal: int,
        messages: list[BaseMessage],
        *,
        turn_id: str,
        message_sequences: list[int] | None = None,
        projection: Mapping[str, object] | None = None,
        response_parts: list[TurnResponsePartDTO] | None = None,
        tool_call_ids: frozenset[str] | None = None,
    ) -> TurnDetailDTO:
        user_messages = [
            message
            for message in messages
            if isinstance(message, HumanMessage) and not self._is_internal(message)
        ]
        first = user_messages[0] if user_messages else messages[0]
        created_at = self._message_time(first)
        final = self._final_message(
            messages,
            projection,
            message_sequences=message_sequences,
        )
        updated_at = (
            self._message_time(final, fallback=created_at)
            if final is not None
            else created_at
        )
        projected_final_text = (
            projection.get("final_response_text") if projection is not None else None
        )
        final_text = (
            self._visible_content_text(final)
            if final is not None
            else projected_final_text
            if isinstance(projected_final_text, str)
            else ""
        )
        assistant_text = [
            text
            for message in messages
            if isinstance(message, AIMessage)
            and message is not final
            and not self._is_internal(message)
            and (text := self._visible_content_text(message))
        ]
        thinking_blocks = self._thinking_blocks(messages)
        if projection is not None:
            raw_thinking = projection.get("thinking_blocks")
            if isinstance(raw_thinking, list):
                thinking_blocks = [
                    TurnThinkingBlockDTO(
                        kind=item["kind"],
                        # SQLite 投影保存完整 reasoning 摘要；DTO 只允许
                        # 有界文本，避免真实模型的长思考内容在组装详情时
                        # 先于统一 budget 截断触发校验异常。
                        text=item.get("text", "")[:4096],
                    )
                    for item in raw_thinking
                    if isinstance(item, Mapping)
                    and item.get("kind") in {"reasoning", "summary", "encrypted"}
                    and isinstance(item.get("text", ""), str)
                ]
            if projection.get("has_encrypted_reasoning") is True and not any(
                block.kind == "encrypted" for block in thinking_blocks
            ):
                thinking_blocks.append(TurnThinkingBlockDTO(kind="encrypted"))
        decoded_tool_items = self._tool_items(
            session_id,
            turn_id,
            messages,
            fallback_timestamp=created_at,
            tool_call_ids=tool_call_ids,
        )
        projected_tool_summary: list[TurnToolSummaryDTO] = []
        projected_tool_items: list[TraceEventDTO] = []
        raw_tool_items = (
            projection.get("tool_items") if projection is not None else None
        )
        if isinstance(raw_tool_items, list):
            projected_call_ids: dict[str, list[str]] = {}
            projected_call_ids_all: list[str] = []
            for raw_item in raw_tool_items:
                if (
                    not isinstance(raw_item, Mapping)
                    or raw_item.get("item_kind") != "tool_call"
                ):
                    continue
                tool_name = raw_item.get("tool_name")
                tool_call_id = raw_item.get("tool_call_id")
                if (
                    tool_call_ids is not None
                    and (
                        not isinstance(tool_call_id, str)
                        or tool_call_id not in tool_call_ids
                    )
                ):
                    continue
                if (
                    isinstance(tool_name, str)
                    and isinstance(tool_call_id, str)
                    and tool_name
                    and tool_call_id
                ):
                    projected_call_ids.setdefault(tool_name, []).append(tool_call_id)
                    projected_call_ids_all.append(tool_call_id)
            for index, raw_item in enumerate(raw_tool_items):
                if not isinstance(raw_item, Mapping):
                    continue
                tool_name = raw_item.get("tool_name")
                if not isinstance(tool_name, str) or not tool_name:
                    tool_name = "tool"
                status = raw_item.get("status")
                if not isinstance(status, str) or not status:
                    status = "unknown"
                tool_call_id = raw_item.get("tool_call_id")
                tool_call_id = (
                    tool_call_id
                    if isinstance(tool_call_id, str) and tool_call_id
                    else (
                        projected_call_ids.get(tool_name, []).pop(0)
                        if raw_item.get("item_kind") == "tool_result"
                        and projected_call_ids.get(tool_name)
                        else (
                            projected_call_ids_all.pop(0)
                            if raw_item.get("item_kind") == "tool_result"
                            and projected_call_ids_all
                            else f"{turn_id}:tool:{index}"
                        )
                    )
                )
                if (
                    tool_call_ids is not None
                    and (
                        not isinstance(tool_call_id, str)
                        or tool_call_id not in tool_call_ids
                    )
                ):
                    continue
                if raw_item.get("item_kind") == "tool_result":
                    projected_call_ids_all = [
                        value
                        for value in projected_call_ids_all
                        if value != tool_call_id
                    ]
                projected_tool_summary.append(
                    TurnToolSummaryDTO(
                        tool_name=tool_name,
                        status=status,
                        tool_call_id=tool_call_id,
                    )
                )
                item_kind = raw_item.get("item_kind")
                event_type = (
                    "tool_call_start" if item_kind == "tool_call" else "tool_call_end"
                )
                projected_tool_items.append(
                    self._tool_event(
                        event_id=f"{turn_id}:tool_summary:{index}",
                        turn_id=turn_id,
                        event_type=event_type,
                        title=f"{'调用工具' if event_type == 'tool_call_start' else '工具结果'} {tool_name}",
                        tool_name=tool_name,
                        part_id=tool_call_id,
                        timestamp=created_at,
                        raw={},
                        session_id=session_id,
                    )
                )
        has_materialized_tool_result = any(
            isinstance(message, ToolMessage) for message in messages
        )
        tool_items = (
            decoded_tool_items
            if has_materialized_tool_result or not projected_tool_items
            else projected_tool_items
        )
        activity_stats = TurnActivityStatsDTO()
        raw_activity_stats = projection.get("activity_stats") if projection else None
        if isinstance(raw_activity_stats, Mapping):
            duration_ms = self._duration_milliseconds(
                projection.get("created_at") if projection else None,
                projection.get("updated_at") if projection else None,
            )
            activity_stats = TurnActivityStatsDTO(
                duration_ms=duration_ms,
                message_count=self._nonnegative_int(raw_activity_stats.get("message_count")),
            )
        tool_summary, tool_summary_truncated = self._bounded_tool_summary(
            projected_tool_summary
            or [
                TurnToolSummaryDTO(
                    tool_name=item.tool_name or "tool",
                    status=item.status,
                    tool_call_id=item.part_id,
                )
                for item in decoded_tool_items
            ]
        )
        detail = TurnDetailDTO(
            turn_id=turn_id,
            job_id=turn_id,
            session_id=session_id,
            ordinal=ordinal,
            revision=1,
            status=JobStatus(
                str(projection.get("status", JobStatus.completed.value))
                if projection is not None
                else JobStatus.completed.value
            ),
            created_at=created_at,
            updated_at=updated_at,
            completed_at=updated_at,
            source_message_ids=[
                self._message_id(item, f"{turn_id}:user:{index}")
                for index, item in enumerate(user_messages)
            ],
            merged_job_ids=[],
            user_messages=[
                self._user_message(session_id, turn_id, item, index)
                for index, item in enumerate(user_messages)
            ],
            response_preview=final_text[:1000],
            preview_truncated=len(final_text) > 1000,
            assistant_text=assistant_text,
            thinking_blocks=thinking_blocks,
            tool_summary=tool_summary,
            tool_summary_truncated=tool_summary_truncated,
            final_response=final_text,
            response_parts=response_parts or [],
            items=tool_items,
            detail_truncated=tool_summary_truncated,
            activity_stats=activity_stats,
        )
        return detail

    def _tool_items(
        self,
        session_id: str,
        turn_id: str,
        messages: list[BaseMessage],
        *,
        fallback_timestamp: datetime,
        tool_call_ids: frozenset[str] | None = None,
    ) -> list[TraceEventDTO]:
        items: list[TraceEventDTO] = []
        tool_names: dict[str, str] = {}
        tool_ids_by_name: dict[str, list[str]] = {}
        tool_ids: list[str] = []
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls or []:
                call_id = call.get("id")
                if (
                    tool_call_ids is not None
                    and (not isinstance(call_id, str) or call_id not in tool_call_ids)
                ):
                    continue
                name = call.get("name")
                if isinstance(call_id, str) and isinstance(name, str) and name:
                    tool_names[call_id] = name
                    tool_ids_by_name.setdefault(name, []).append(call_id)
                    tool_ids.append(call_id)
        for message_index, message in enumerate(messages):
            timestamp = self._message_time(message, fallback=fallback_timestamp)
            if isinstance(message, AIMessage):
                for call_index, call in enumerate(message.tool_calls or []):
                    name = str(call.get("name") or "unknown_tool")
                    args = call.get("args", {})
                    call_id = str(call.get("id") or f"{turn_id}:call:{call_index}")
                    if (
                        tool_call_ids is not None
                        and call_id not in tool_call_ids
                    ):
                        continue
                    items.append(
                        self._tool_event(
                            event_id=f"{turn_id}:tool_call:{message_index}:{call_index}",
                            turn_id=turn_id,
                            event_type="tool_call_start",
                            title=f"调用工具 {name}",
                            tool_name=name,
                            part_id=call_id,
                            timestamp=timestamp,
                            raw={
                                "payload": {
                                    "args": args,
                                    "id": call_id,
                                    "tool_name": name,
                                }
                            },
                            session_id=session_id,
                        )
                    )
            elif isinstance(message, ToolMessage):
                name = message.name or tool_names.get(message.tool_call_id) or "tool"
                tool_call_id = message.tool_call_id
                if (
                    tool_call_ids is not None
                    and (
                        not isinstance(tool_call_id, str)
                        or tool_call_id not in tool_call_ids
                    )
                ):
                    continue
                if not tool_call_id and tool_ids_by_name.get(name):
                    tool_call_id = tool_ids_by_name[name].pop(0)
                if not tool_call_id and tool_ids:
                    tool_call_id = tool_ids.pop(0)
                if tool_call_id in tool_ids:
                    tool_ids.remove(tool_call_id)
                tool_call_id = tool_call_id or f"{turn_id}:tool-result:{message_index}"
                items.append(
                    self._tool_event(
                        event_id=f"{turn_id}:tool_result:{message_index}",
                        turn_id=turn_id,
                        event_type="tool_call_end",
                        title=f"工具结果 {name}",
                        tool_name=name,
                        part_id=tool_call_id,
                        timestamp=timestamp,
                        raw={
                            "payload": {
                                "result": self._content_text(message),
                                "tool_call_id": tool_call_id,
                                "tool_name": name,
                            }
                        },
                        session_id=session_id,
                    )
                )
        return items

    @staticmethod
    def _tool_event(
        *,
        event_id: str,
        turn_id: str,
        event_type: str,
        title: str,
        tool_name: str,
        part_id: str | None,
        timestamp: datetime,
        raw: dict[str, object],
        session_id: str,
    ) -> TraceEventDTO:
        return TraceEventDTO(
            event_id=event_id,
            part_id=part_id,
            session_id=session_id,
            job_id=turn_id,
            type=event_type,
            phase="tool",
            title=title,
            content="",
            status="completed",
            tool_name=tool_name,
            timestamp=timestamp,
            raw=raw,
        )

    @staticmethod
    def _bounded_tool_summary(
        items: list[TurnToolSummaryDTO],
    ) -> tuple[list[TurnToolSummaryDTO], bool]:
        if len(items) <= _TOOL_SUMMARY_LIMIT:
            return items, False
        head_count = _TOOL_SUMMARY_LIMIT // 2
        tail_count = _TOOL_SUMMARY_LIMIT - head_count
        return [*items[:head_count], *items[-tail_count:]], True

    def _project(
        self,
        detail: TurnDetailDTO,
        include: tuple[str, ...],
        budget: DetailReadBudget,
    ) -> TurnDetailDTO:
        fields = set(include)
        user_messages = detail.user_messages if "user" in fields else []
        if "internal" in fields:
            user_messages = detail.user_messages
        if "metadata" not in fields:
            user_messages = [
                message.model_copy(update={"metadata": {}}) for message in user_messages
            ]
        projected_user_messages: list[TurnUserMessageDTO] = []
        output_truncated = False
        for message in user_messages:
            content, content_truncated = self._bounded_text(message.content, budget)
            if content_truncated:
                output_truncated = True
            projected_user_messages.append(
                message.model_copy(
                    update={
                        "content": content,
                        "content_truncated": (
                            message.content_truncated or content_truncated
                        ),
                    }
                )
            )
        items: list[TraceEventDTO] = []
        tool_detail_requested = bool(
            fields & {"tool_summary", "tool_call", "tool_result"}
        )
        expected_items = 0
        for item in detail.items:
            is_call = item.type == "tool_call_start"
            requested = "tool_call" in fields if is_call else "tool_result" in fields
            summary_requested = "tool_summary" in fields
            if not requested and not summary_requested:
                continue
            expected_items += 1
            raw = item.raw if requested else {}
            content = item.content if requested else ""
            bounded_raw = raw
            if requested:
                encoded = json.dumps(raw, ensure_ascii=False, default=str)
                if not budget.can_add(
                    byte_count=len(encoded.encode("utf-8")),
                    char_count=len(encoded),
                ):
                    output_truncated = True
                    break
                budget.add(
                    byte_count=len(encoded.encode("utf-8")),
                    char_count=len(encoded),
                )
                content, content_truncated = self._bounded_text(content, budget)
                if content_truncated:
                    output_truncated = True
            items.append(
                item.model_copy(update={"raw": bounded_raw, "content": content})
            )
        final_response = ""
        if "final_response" in fields or "assistant" in fields:
            final_response, content_truncated = self._bounded_text(
                detail.final_response,
                budget,
            )
            output_truncated = output_truncated or content_truncated
        assistant_text: list[str] = []
        if "assistant_text" in fields or "assistant" in fields:
            for value in detail.assistant_text:
                bounded, content_truncated = self._bounded_text(value, budget)
                assistant_text.append(bounded)
                output_truncated = output_truncated or content_truncated
        thinking_blocks: list[TurnThinkingBlockDTO] = []
        if fields & {"thinking", "reasoning_summary", "reasoning_detail"}:
            for block in detail.thinking_blocks:
                if block.kind == "encrypted" and "encrypted_reasoning_meta" in fields:
                    thinking_blocks.append(block)
                    continue
                if block.kind == "encrypted":
                    continue
                if block.kind == "reasoning" and not fields & {
                    "reasoning_detail",
                    "thinking",
                }:
                    continue
                if block.kind == "summary" and not fields & {
                    "reasoning_summary",
                    "reasoning_detail",
                    "thinking",
                }:
                    continue
                bounded, content_truncated = self._bounded_text(block.text, budget)
                thinking_blocks.append(block.model_copy(update={"text": bounded}))
                output_truncated = output_truncated or content_truncated
        response_parts = []
        for part in detail.response_parts:
            requested = (
                "final_response"
                if part.kind == "final_text"
                or (
                    part.partial is True
                    and part.completion_reason == "user_interrupt"
                )
                else "text"
                if part.kind == "text"
                else "reasoning_detail"
                if part.kind == "reasoning"
                else "reasoning_summary"
                if part.kind == "reasoning_summary"
                else "encrypted_reasoning_meta"
                if part.kind == "reasoning_encrypted"
                else "tool_call"
                if part.kind == "tool_call"
                else "tool_result"
            )
            reasoning_requested = (
                part.kind == "reasoning"
                and bool(fields & {"thinking", "reasoning_detail"})
            ) or (
                part.kind == "reasoning_summary"
                and bool(fields & {"thinking", "reasoning_summary", "reasoning_detail"})
            ) or (
                part.kind == "reasoning_encrypted"
                and "encrypted_reasoning_meta" in fields
            )
            if requested not in fields and not (
                requested == "final_response" and "assistant" in fields
            ) and not reasoning_requested and not (
                requested in {"tool_call", "tool_result"}
                and "tool_summary" in fields
                and part.projection == "summary"
            ):
                continue
            text, content_truncated = self._bounded_text(part.text, budget)
            response_parts.append(
                part.model_copy(update={"text": text})
            )
            output_truncated = output_truncated or content_truncated
        return detail.model_copy(
            update={
                "user_messages": projected_user_messages,
                "final_response": final_response,
                "assistant_text": assistant_text,
                "thinking_blocks": thinking_blocks,
                # 显式详情超出预算时仍保留工具名和状态，避免 UI 只得到
                # detail_truncated=true 却失去可解释的工具摘要。
                "tool_summary": (
                    detail.tool_summary
                    if "tool_summary" in fields
                    or (tool_detail_requested and output_truncated)
                    else []
                ),
                "response_preview": final_response[:1000],
                "preview_truncated": len(final_response) > 1000,
                "items": items,
                "response_parts": response_parts,
                "detail_truncated": output_truncated
                or detail.detail_truncated
                or (tool_detail_requested and len(items) < expected_items),
            }
        )

    @staticmethod
    def _bounded_text(
        value: str,
        budget: DetailReadBudget,
    ) -> tuple[str, bool]:
        if not value:
            return "", False
        limit = min(len(value), budget.limits.item_chars)
        candidate = value[:limit]
        encoded = json.dumps(candidate, ensure_ascii=False)
        while candidate and not budget.can_add(
            byte_count=len(encoded.encode("utf-8")),
            char_count=len(encoded),
        ):
            limit //= 2
            candidate = value[:limit]
            encoded = json.dumps(candidate, ensure_ascii=False)
        if not budget.can_add(
            byte_count=len(encoded.encode("utf-8")),
            char_count=len(encoded),
        ):
            return "", True
        budget.add(
            byte_count=len(encoded.encode("utf-8")),
            char_count=len(encoded),
        )
        return candidate, len(candidate) < len(value)

    @staticmethod
    def _summary(detail: TurnDetailDTO) -> TurnSummaryDTO:
        return TurnSummaryDTO(
            **detail.model_dump(
                mode="python",
                include={
                    "turn_id",
                    "job_id",
                    "session_id",
                    "ordinal",
                    "revision",
                    "status",
                    "created_at",
                    "updated_at",
                    "completed_at",
                },
            ),
            source_message_ids=detail.source_message_ids,
            source_message_count=len(detail.source_message_ids),
            user_messages=[
                TurnUserMessageSummaryDTO(
                    message_id=item.message_id,
                    preview=item.content[:500],
                    content_truncated=len(item.content) > 500,
                    attachment_count=len(item.attachments),
                    created_at=item.created_at,
                )
                for item in detail.user_messages
            ],
            user_message_count=len(detail.user_messages),
            response_preview=detail.response_preview,
            preview_truncated=detail.preview_truncated,
            item_count=len(detail.items),
            thinking_blocks=detail.thinking_blocks,
            tool_summary=detail.tool_summary,
            tool_summary_truncated=detail.tool_summary_truncated,
            response_parts=detail.response_parts[:128],
            activity_stats=detail.activity_stats,
        )

    @staticmethod
    def _has_public_content(detail: TurnDetailDTO) -> bool:
        """控制 Turn 可能只有 internal 消息，不应成为用户看到的最新 Turn。"""
        return bool(
            detail.user_messages
            or detail.assistant_text
            or detail.thinking_blocks
            or detail.tool_summary
            or detail.final_response
            or detail.items
        )

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _duration_milliseconds(start: object, end: object) -> int | None:
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        try:
            started_at = datetime.fromisoformat(start)
            ended_at = datetime.fromisoformat(end)
        except ValueError:
            return None
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=UTC)
        return max(0, int((ended_at - started_at).total_seconds() * 1000))

    @staticmethod
    def _bounded_limit(requested: int | None, configured: int) -> int:
        return min(requested or configured, 64)

    @staticmethod
    def _anchor_ordinal(cursor: TurnCursorDTO | None, default: int) -> int:
        if cursor is None:
            return default
        return int(cursor.anchor_turn_id)

    def _decode_cursor(
        self,
        session_id: str,
        rollout_id: str,
        projection_epoch: int,
        cursor: str | None,
    ) -> TurnCursorDTO | None:
        if cursor is None:
            return None
        value = self._decode_cursor_payload(cursor)
        if (
            value.get("session_id") != session_id
            or value.get("rollout_id") != rollout_id
        ):
            raise InvalidTurnCursorError("历史游标不属于当前 rollout")
        raw_epoch = value.get("projection_epoch")
        if raw_epoch != projection_epoch:
            raise StaleTurnCursorError(
                session_id=session_id,
                cursor_epoch=int(raw_epoch) if isinstance(raw_epoch, int) else 0,
                current_epoch=projection_epoch,
            )
        raw_anchor = value.get("anchor_ordinal")
        raw_direction = value.get("direction")
        raw_stage = value.get("stage")
        if (
            isinstance(raw_anchor, bool)
            or not isinstance(raw_anchor, int)
            or raw_anchor < 1
            or raw_direction not in {"before", "after"}
            or isinstance(raw_stage, bool)
            or not isinstance(raw_stage, int)
            or raw_stage < 0
        ):
            raise InvalidTurnCursorError("历史游标内容非法")
        return TurnCursorDTO(
            session_id=session_id,
            projection_epoch=projection_epoch,
            anchor_turn_id=str(raw_anchor),
            direction=raw_direction,
            stage=raw_stage,
        )

    @staticmethod
    def _encode_cursor(
        *,
        session_id: str,
        rollout_id: str,
        projection_epoch: int,
        anchor_ordinal: int,
        direction: str,
        stage: int,
    ) -> str:
        payload = json.dumps(
            {
                "version": 1,
                "session_id": session_id,
                "rollout_id": rollout_id,
                "projection_epoch": projection_epoch,
                "anchor_ordinal": anchor_ordinal,
                "direction": direction,
                "stage": stage,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor_payload(cursor: str) -> dict[str, object]:
        try:
            payload = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            value = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidTurnCursorError("历史游标不是合法的不透明值") from error
        if not isinstance(value, dict) or value.get("version") != 1:
            raise InvalidTurnCursorError("历史游标版本不受支持")
        return value

    @staticmethod
    def _message_metadata(message: BaseMessage) -> dict[str, object]:
        raw = message.response_metadata.get("message_metadata")
        return dict(raw) if isinstance(raw, Mapping) else {}

    @staticmethod
    def _string_value(
        value: Mapping[str, object],
        key: str,
        fallback: str | None,
    ) -> str | None:
        raw = value.get(key)
        return raw if isinstance(raw, str) and raw else fallback

    @staticmethod
    def _message_id(message: BaseMessage, fallback: str) -> str:
        if isinstance(message.id, str) and message.id:
            return message.id
        raw = message.response_metadata.get("message_id")
        return raw if isinstance(raw, str) and raw else fallback

    @staticmethod
    def _message_time(
        message: BaseMessage | None,
        *,
        fallback: datetime | None = None,
    ) -> datetime:
        if message is not None:
            raw = message.response_metadata.get("created_at")
            if isinstance(raw, datetime):
                return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
            if isinstance(raw, str) and raw:
                parsed = datetime.fromisoformat(raw)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return fallback or datetime.now(UTC)

    @staticmethod
    def _content_text(message: BaseMessage | None) -> str:
        if message is None:
            return ""
        content = message.content
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, default=str)

    @staticmethod
    def _visible_content_text(message: BaseMessage | None) -> str:
        if message is None:
            return ""
        return litellm_visible_text(message.content)

    @classmethod
    def _thinking_blocks(
        cls, messages: list[BaseMessage]
    ) -> list[TurnThinkingBlockDTO]:
        result: list[TurnThinkingBlockDTO] = []
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            content = message.content
            for row in reasoning_projection_rows(content):
                kind = row.get("kind")
                text = row.get("text")
                if kind == "encrypted":
                    result.append(TurnThinkingBlockDTO(kind="encrypted"))
                elif (
                    kind in {"reasoning", "summary"}
                    and isinstance(text, str)
                    and text.strip()
                ):
                    result.append(
                        TurnThinkingBlockDTO(
                            kind=str(kind),
                            text=text.strip()[:4096],
                        )
                    )
                # 同一 provider item 的可见思考和 encrypted carrier 已在
                # canonical 投影中合并，不能再为 encrypted carrier 追加重复卡片。
        # 详情 DTO 不再以 thinking block 数量限制历史；文本预算在显式投影阶段
        # 统一处理，避免第 33 个及之后的思考块在无 SQLite 投影路径中丢失。
        return result

    @staticmethod
    def _is_internal(message: BaseMessage) -> bool:
        metadata = message.response_metadata
        if metadata.get("internal") is True:
            return True
        message_metadata = metadata.get("message_metadata")
        return (
            isinstance(message_metadata, Mapping)
            and message_metadata.get("internal") is True
        )

    def _final_message(
        self,
        messages: list[BaseMessage],
        projection: Mapping[str, object] | None = None,
        *,
        message_sequences: list[int] | None = None,
    ) -> AIMessage | None:
        final_sequence = (
            projection.get("final_message_sequence") if projection is not None else None
        )
        final_message_id = (
            projection.get("final_message_id") if projection is not None else None
        )
        if isinstance(final_sequence, int) and not isinstance(final_sequence, bool):
            if message_sequences is not None and len(message_sequences) != len(messages):
                raise RuntimeError("final message sequence 与消息数量不一致")
            candidates = (
                zip(message_sequences, messages) if message_sequences is not None else ()
            )
            for sequence, message in candidates:
                if sequence != final_sequence:
                    continue
                if not isinstance(message, AIMessage) or self._is_internal(message):
                    raise RuntimeError(
                        "SQLite final_message_sequence 未指向可见 AIMessage: "
                        f"sequence={final_sequence}"
                    )
                if isinstance(final_message_id, str) and final_message_id:
                    actual_id = self._message_id(message, "")
                    if actual_id != final_message_id:
                        raise RuntimeError(
                            "SQLite finalization 指针与消息 ID 不一致: "
                            f"sequence={final_sequence} expected={final_message_id} actual={actual_id}"
                        )
                return message
        if isinstance(final_message_id, str) and final_message_id:
            for message in messages:
                if (
                    isinstance(message, AIMessage)
                    and self._message_id(message, "") == final_message_id
                    and not self._is_internal(message)
                ):
                    return message
        for message in reversed(messages):
            if (
                isinstance(message, AIMessage)
                and not message.tool_calls
                and not self._is_internal(message)
                and message.response_metadata.get("phase") == "final_answer"
            ):
                return message
        for message in reversed(messages):
            if (
                isinstance(message, AIMessage)
                and not message.tool_calls
                and not self._is_internal(message)
            ):
                return message
        return None

    def _user_message(
        self,
        session_id: str,
        turn_id: str,
        message: BaseMessage,
        index: int,
    ) -> TurnUserMessageDTO:
        response_metadata = dict(message.response_metadata)
        projection = user_content_projection(message.content, response_metadata)
        attachments = [
            TurnAttachmentDTO.model_validate(
                {
                    str(key): value
                    for key, value in item.items()
                    if str(key) != "data_url"
                }
            )
            for item in projection.attachments
        ]
        response_metadata.pop("display_content", None)
        response_metadata.pop("attachments", None)
        return TurnUserMessageDTO(
            message_id=self._message_id(message, f"{turn_id}:user:{index}"),
            content=projection.visible_text,
            content_truncated=False,
            attachments=attachments,
            metadata=response_metadata,
            created_at=self._message_time(message),
        )


__all__ = ["RolloutHistoryReader"]
