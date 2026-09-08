"""把历史游标/方向绑定到同一个已提交 snapshot 的有界查询。"""

from __future__ import annotations

import base64
import json

from app.core.history_loading import (
    HistoryLoadingConfig,
    default_history_loading_config,
)
from app.schemas.internal_v2.turn import (
    TurnCursorDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
)
from app.services.infrastructure.rollout_history.snapshot import (
    IndexedHistory,
    IndexedTurnSpan,
)
from app.services.infrastructure.turn_history.models import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)


class HistoryPageReadMixin:
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
    def _span_from_row(row: tuple[str, int, int, int]) -> IndexedTurnSpan:
        return IndexedTurnSpan(
            turn_id=row[0],
            first_sequence=row[1],
            last_sequence=row[2],
            ordinal=row[3],
        )

    def _indexed_before_cursor(
        self,
        session_id: str,
        rollout_id: str,
        projection_epoch: int,
        selected: list[IndexedTurnSpan],
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
        selected: list[IndexedTurnSpan],
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
