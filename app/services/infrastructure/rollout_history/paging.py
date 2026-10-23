"""从同一个已提交 snapshot 执行 rollout history 有界分页读取。"""

from __future__ import annotations

from app.core.history_loading import (
    HistoryLoadingConfig,
    default_history_loading_config,
)
from app.schemas.internal_v2.turn import (
    TurnCursorDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnInclude,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import ContextChain
from app.services.infrastructure.rollout_context.storage.primitives import (
    InternalExecutionHistoryPage,
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_history.cursor import HistoryCursorMixin
from app.services.infrastructure.rollout_history.snapshot import (
    IndexedHistory,
    IndexedTurnSpan,
)
from app.services.infrastructure.turn_history.models import (
    InvalidTurnCursorError,
    StaleTurnReferenceError,
)

_INTERNAL_HISTORY_PAGE_LIMIT = 256

class HistoryPageReadMixin(HistoryCursorMixin):
    def _load_indexed_history(
        self,
        session_id: str,
        request: TurnHistoryLoadRequest,
        indexed: IndexedHistory,
        *,
        policy: HistoryLoadingConfig | None,
    ) -> TurnHistoryPageDTO:
        rollout_id = indexed.rollout_id
        projection_epoch = indexed.projection_epoch
        snapshot = indexed.snapshot
        configured = policy or default_history_loading_config()
        if request.turn_ids is not None and request.cursor is not None:
            raise InvalidTurnCursorError("按 turn_ids 加载详情不能使用历史分页游标")
        bounds = (
            self._context_reader.read_history_snapshot_bounds(snapshot, indexed.chain)
            if indexed.view_id is not None
            else None
        )
        if bounds is not None and (
            bounds.view_id != indexed.view_id
            or bounds.history_view_revision != snapshot.manifest.history_view_revision
        ):
            raise RuntimeError("rollout history bounds 与同一 snapshot 的 view owner 不一致")
        decoded_cursor = self._decode_cursor(
            session_id=session_id,
            thread_id=snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns,
            rollout_id=rollout_id,
            projection_epoch=projection_epoch,
            view_id=indexed.view_id,
            history_view_revision=snapshot.manifest.history_view_revision,
            turn_read_upper_bound=(
                bounds.upper_turn_ordinal if bounds is not None else None
            ),
            logical_item_read_upper_bound=(
                bounds.upper_logical_item_ordinal if bounds is not None else None
            ),
            expected_direction=(
                "before"
                if request.direction in {"before", "older"}
                else request.direction
                if request.direction == "after"
                else None
            ),
            cursor=request.cursor,
        )
        binding = self._cursor_binding(
            request,
            configured,
            decoded_cursor,
            history_view_revision=(
                snapshot.manifest.history_view_revision
            ),
            turn_read_upper_bound=(
                bounds.upper_turn_ordinal if bounds is not None else None
            ),
            logical_item_read_upper_bound=(
                bounds.upper_logical_item_ordinal if bounds is not None else None
            ),
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
        include = binding.effective_include

        if binding.execution_id is not None:
            internal_page = self._read_internal_page(
                snapshot, indexed.chain, include=include, direction="before",
                anchor_logical_item_ordinal=decoded_cursor.logical_item_ordinal if decoded_cursor else None,
                lower_logical_item_ordinal=None,
                upper_logical_item_ordinal=binding.logical_item_read_upper_bound,
                snapshot_upper_logical_item_ordinal=binding.logical_item_read_upper_bound,
                execution_id=binding.execution_id,
            )
            before_cursor = self._indexed_before_cursor(
                session_id, indexed, binding=binding, stage=0, turn_ordinal=None,
                logical_item_ordinal=internal_page.entries[0].logical_item_ordinal,
            ) if internal_page.has_more else None
            return self._indexed_page(
                session_id, [], include=include, next_cursor=before_cursor,
                has_more=internal_page.has_more, before_cursor=before_cursor,
                has_before=internal_page.has_more, projection_epoch=projection_epoch,
                snapshot=snapshot, chain=indexed.chain,
                internal_entries=internal_page.entries,
            )

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
                known_turns = self._context_reader.read_existing_turn_ids(
                    snapshot,
                    missing,
                )
                stale = [
                    turn_id for turn_id in missing if turn_id in known_turns
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
        if request.direction == "tail":
            limit = self._bounded_limit(request.turns, configured.initial_turns)
            turn_rows, turn_has_more = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="tail",
                anchor_ordinal=None,
                upper_ordinal=bounds.upper_turn_ordinal,
                limit=limit,
            )
            internal_page = self._read_internal_page(
                snapshot,
                indexed.chain,
                include=include,
                direction="before",
                anchor_logical_item_ordinal=None,
                lower_logical_item_ordinal=None,
                upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
                snapshot_upper_logical_item_ordinal=binding.logical_item_read_upper_bound,
            )
            selected = [self._span_from_row(row) for row in turn_rows]
            has_before = turn_has_more or internal_page.has_more
            before_cursor = self._indexed_before_cursor(
                session_id,
                indexed,
                binding=binding,
                stage=0,
                turn_ordinal=(selected[0].ordinal if turn_has_more and selected else None),
                logical_item_ordinal=(
                    internal_page.entries[0].logical_item_ordinal
                    if internal_page.has_more and internal_page.entries
                    else None
                ),
            ) if has_before else None
            return self._indexed_page(
                session_id,
                selected,
                include=include,
                next_cursor=before_cursor,
                has_more=has_before,
                before_cursor=before_cursor,
                has_before=has_before,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
                internal_entries=internal_page.entries,
            )

        if request.direction in {"before", "older", "head", "after"}:
            is_before = request.direction in {"before", "older"}
            direction = (
                "before"
                if is_before
                else "head"
                if request.direction == "head"
                else "after"
            )
            default_limit = (
                configured.anchor_before_turns
                if is_before
                else configured.anchor_after_turns
            )
            limit = self._bounded_limit(request.turns, default_limit)
            lane_active = decoded_cursor is None or decoded_cursor.turn_ordinal is not None
            if request.direction == "head" and decoded_cursor is not None:
                lane_active = False
            if bounds.upper_turn_ordinal is None or not lane_active:
                turn_rows, turn_has_more = [], False
            else:
                anchor = (
                    decoded_cursor.turn_ordinal
                    if decoded_cursor is not None
                    else bounds.upper_turn_ordinal + 1
                    if is_before
                    else 0
                )
                turn_rows, turn_has_more = self._context_reader.read_context_turn_page(
                    snapshot,
                    indexed.chain,
                    direction=direction,
                    anchor_ordinal=anchor if direction != "head" else None,
                    upper_ordinal=bounds.upper_turn_ordinal,
                    limit=limit,
                )
            internal_lane_active = (
                decoded_cursor is None
                or decoded_cursor.logical_item_ordinal is not None
            )
            if request.direction == "head" and decoded_cursor is not None:
                internal_lane_active = False
            internal_page = (
                self._read_internal_page(
                    snapshot,
                    indexed.chain,
                    include=include,
                    direction="before" if is_before else "after",
                    anchor_logical_item_ordinal=(
                        decoded_cursor.logical_item_ordinal
                        if decoded_cursor is not None
                        else None
                    ),
                    lower_logical_item_ordinal=None,
                    upper_logical_item_ordinal=bounds.upper_logical_item_ordinal,
                    snapshot_upper_logical_item_ordinal=binding.logical_item_read_upper_bound,
                )
                if internal_lane_active
                else InternalExecutionHistoryPage(entries=(), has_more=False)
            )
            selected = [self._span_from_row(row) for row in turn_rows]
            has_more = turn_has_more or internal_page.has_more
            next_cursor = None
            if has_more:
                turn_anchor = (
                    selected[0].ordinal if is_before else selected[-1].ordinal
                ) if turn_has_more and selected else None
                item_anchor = (
                    internal_page.entries[0].logical_item_ordinal
                    if is_before
                    else internal_page.entries[-1].logical_item_ordinal
                ) if internal_page.has_more and internal_page.entries else None
                cursor_builder = (
                    self._indexed_before_cursor if is_before else self._indexed_after_cursor
                )
                next_cursor = cursor_builder(
                    session_id,
                    indexed,
                    binding=binding,
                    stage=stage + (1 if decoded_cursor is not None else 0),
                    turn_ordinal=turn_anchor,
                    logical_item_ordinal=item_anchor,
                )
            return self._indexed_page(
                session_id,
                selected,
                include=include,
                next_cursor=next_cursor,
                has_more=has_more,
                before_cursor=next_cursor if is_before else None,
                after_cursor=next_cursor if not is_before else None,
                has_before=has_more if is_before else False,
                has_after=has_more if not is_before else False,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
                internal_entries=internal_page.entries,
            )

        if request.direction == "around":
            if binding.anchor_turn_id is not None:
                anchor_rows = self._context_reader.read_context_turn_ids(
                    snapshot,
                    indexed.chain,
                    [binding.anchor_turn_id],
                )
                if not anchor_rows:
                    known_turns = self._context_reader.read_existing_turn_ids(
                        snapshot,
                        [binding.anchor_turn_id],
                    )
                    if binding.anchor_turn_id in known_turns:
                        raise StaleTurnReferenceError(
                            session_id=session_id,
                            turn_ids=[binding.anchor_turn_id],
                        )
                    raise KeyError(f"rollout Turn 不存在: {binding.anchor_turn_id}")
                anchor = anchor_rows[0][3]
            else:
                raise InvalidTurnCursorError("window 历史游标缺少冻结 anchor_turn_id")
            before = binding.window_before_turns
            after = binding.window_after_turns
            if before is None or after is None:
                raise InvalidTurnCursorError("window 历史游标缺少冻结窗口")
            before_rows, before_more = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="before",
                anchor_ordinal=anchor,
                upper_ordinal=bounds.upper_turn_ordinal,
                limit=before or 1,
            ) if bounds.upper_turn_ordinal is not None else ([], False)
            after_rows, after_more = self._context_reader.read_context_turn_page(
                snapshot,
                indexed.chain,
                direction="after",
                anchor_ordinal=anchor,
                upper_ordinal=bounds.upper_turn_ordinal,
                limit=after or 1,
            ) if bounds.upper_turn_ordinal is not None else ([], False)
            anchor_span = self._span_from_row(anchor_rows[0])
            before_selected = (
                [self._span_from_row(row) for row in before_rows]
                if before
                else []
            )
            after_selected = (
                [self._span_from_row(row) for row in after_rows]
                if after
                else []
            )
            before_has_more = before_more if before else bool(before_rows)
            after_has_more = after_more if after else bool(after_rows)
            selected = [*before_selected, anchor_span, *after_selected]
            turn_before_anchor = (
                before_selected[0].ordinal if before_selected else anchor_span.ordinal
            )
            turn_after_anchor = (
                after_selected[-1].ordinal if after_selected else anchor_span.ordinal
            )
            turn_window = self._context_reader.resolve_context_turn_item_window(
                snapshot,
                indexed.chain,
                first_logical_turn_ordinal=selected[0].ordinal,
                last_logical_turn_ordinal=selected[-1].ordinal,
            )
            internal_page = self._read_internal_page(
                snapshot,
                indexed.chain,
                include=include,
                direction="after",
                anchor_logical_item_ordinal=None,
                lower_logical_item_ordinal=turn_window.lower_logical_item_ordinal,
                upper_logical_item_ordinal=turn_window.upper_logical_item_ordinal,
                snapshot_upper_logical_item_ordinal=binding.logical_item_read_upper_bound,
            )
            internal_before = False
            internal_after = internal_page.has_more
            before_item_anchor: int | None = None
            after_item_anchor: int | None = None
            if turn_window.lower_logical_item_ordinal is not None:
                before_internal = self._read_internal_page(
                    snapshot,
                    indexed.chain,
                    include=include,
                    direction="before",
                    anchor_logical_item_ordinal=(
                        turn_window.lower_logical_item_ordinal
                    ),
                    lower_logical_item_ordinal=None,
                    upper_logical_item_ordinal=(
                        bounds.upper_logical_item_ordinal
                    ),
                    snapshot_upper_logical_item_ordinal=binding.logical_item_read_upper_bound,
                    limit=1,
                )
                internal_before = bool(before_internal.entries)
                if internal_before:
                    before_item_anchor = turn_window.lower_logical_item_ordinal
                after_internal = self._read_internal_page(
                    snapshot,
                    indexed.chain,
                    include=include,
                    direction="after",
                    anchor_logical_item_ordinal=(
                        turn_window.upper_logical_item_ordinal
                    ),
                    lower_logical_item_ordinal=None,
                    upper_logical_item_ordinal=(
                        bounds.upper_logical_item_ordinal
                    ),
                    snapshot_upper_logical_item_ordinal=binding.logical_item_read_upper_bound,
                    limit=1,
                ) if turn_window.upper_logical_item_ordinal is not None else InternalExecutionHistoryPage(
                    entries=(),
                    has_more=False,
                )
                internal_after = internal_after or bool(after_internal.entries)
                if internal_page.has_more and internal_page.entries:
                    after_item_anchor = internal_page.entries[-1].logical_item_ordinal
                elif after_internal.entries:
                    after_item_anchor = turn_window.upper_logical_item_ordinal
            has_before = before_has_more or internal_before
            has_after = after_has_more or internal_after
            before_cursor = (
                self._indexed_before_cursor(
                    session_id,
                    indexed,
                    binding=binding,
                    stage=0,
                    turn_ordinal=turn_before_anchor if before_has_more else None,
                    logical_item_ordinal=before_item_anchor,
                )
                if has_before
                else None
            )
            after_cursor = (
                self._indexed_after_cursor(
                    session_id,
                    indexed,
                    binding=binding,
                    stage=0,
                    turn_ordinal=turn_after_anchor if after_has_more else None,
                    logical_item_ordinal=after_item_anchor,
                )
                if has_after
                else None
            )
            return self._indexed_page(
                session_id,
                selected,
                include=include,
                next_cursor=None,
                has_more=False,
                before_cursor=before_cursor,
                after_cursor=after_cursor,
                has_before=has_before,
                has_after=has_after,
                projection_epoch=projection_epoch,
                snapshot=snapshot,
                chain=indexed.chain,
                internal_entries=internal_page.entries,
            )

        raise InvalidTurnCursorError(f"不支持的历史方向: {request.direction}")

    def _read_internal_page(
        self,
        snapshot: RolloutReadSnapshot,
        chain: ContextChain,
        *,
        include: tuple[TurnInclude, ...],
        direction: str,
        anchor_logical_item_ordinal: int | None,
        lower_logical_item_ordinal: int | None,
        upper_logical_item_ordinal: int | None,
        snapshot_upper_logical_item_ordinal: int | None,
        limit: int = _INTERNAL_HISTORY_PAGE_LIMIT,
        execution_id: str | None = None,
    ) -> InternalExecutionHistoryPage:
        if "internal" not in include or upper_logical_item_ordinal is None:
            return InternalExecutionHistoryPage(entries=(), has_more=False)
        return self._context_reader.read_internal_execution_history_page(
            snapshot,
            chain,
            direction=direction,
            anchor_logical_item_ordinal=anchor_logical_item_ordinal,
            lower_logical_item_ordinal=lower_logical_item_ordinal,
            upper_logical_item_ordinal=upper_logical_item_ordinal,
            snapshot_upper_logical_item_ordinal=snapshot_upper_logical_item_ordinal,
            limit=limit,
            execution_id=execution_id,
        )

    @staticmethod
    def _span_from_row(row: tuple[str, int, int, int, int]) -> IndexedTurnSpan:
        return IndexedTurnSpan(
            turn_id=row[0],
            first_sequence=row[1],
            last_sequence=row[2],
            ordinal=row[3],
            root_logical_item_ordinal=row[4],
        )

    @staticmethod
    def _bounded_limit(requested: int | None, configured: int) -> int:
        return min(requested or configured, 64)

    @staticmethod
    def _anchor_ordinal(cursor: TurnCursorDTO | None, default: int) -> int:
        if cursor is None:
            return default
        return cursor.turn_ordinal if cursor.turn_ordinal is not None else default
