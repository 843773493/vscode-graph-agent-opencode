from __future__ import annotations

import base64

from app.schemas.public_v2.turn import (
    TurnCursorDTO,
    TurnDetailBatchDTO,
    TurnDetailDTO,
    TurnPageDTO,
    TurnSummaryDTO,
)

from .files import TurnHistoryFiles
from .models import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    TurnIndex,
    TurnManifest,
)
from .summary import to_turn_summary
from .timeline import TurnTimeline


class TurnHistoryReader:
    """从已恢复的 Turn 文件读取 summary、detail 和稳定 cursor 页。"""

    def __init__(self, files: TurnHistoryFiles, timeline: TurnTimeline) -> None:
        self._files = files
        self._timeline = timeline

    def get_details(
        self,
        session_id: str,
        turn_ids: list[str],
        *,
        manifest: TurnManifest,
    ) -> TurnDetailBatchDTO:
        if not 1 <= len(turn_ids) <= 4:
            raise ValueError("Turn detail 批量数量必须在 1 到 4 之间")
        if len(set(turn_ids)) != len(turn_ids):
            raise ValueError("Turn detail 批量 ID 不能重复")
        items: list[TurnDetailDTO] = []
        for turn_id in turn_ids:
            record = self._files.read_turn_record(
                session_id,
                turn_id,
                required=True,
            )
            timeline_size = self._files.timeline_path(session_id).stat().st_size
            self._timeline.validate_record_anchor(
                session_id,
                record.turn.turn_id,
                timeline_start=record.timeline_start,
                timeline_end=record.timeline_end,
                expected_ordinal=record.turn.ordinal,
                committed_size=timeline_size,
            )
            if not record.visible:
                raise KeyError(
                    f"Turn 不可见: session_id={session_id}, turn_id={turn_id}"
                )
            items.append(record.turn)
        return TurnDetailBatchDTO(
            items=items,
            projection_epoch=manifest.projection_epoch,
        )

    def list_summaries(
        self,
        session_id: str,
        *,
        manifest: TurnManifest,
        index: TurnIndex,
        limit: int,
        cursor: str | None,
    ) -> TurnPageDTO:
        if not 1 <= limit <= 20:
            raise ValueError("Turn summary 分页 limit 必须在 1 到 20 之间")
        if cursor is None and limit == 1:
            return self._latest_summary_page(
                session_id,
                manifest=manifest,
                index=index,
            )
        end_offset = index.timeline_size
        if cursor is not None:
            cursor_value = self._decode_cursor(cursor)
            if cursor_value.session_id != session_id:
                raise ValueError("Turn cursor 不属于当前会话")
            if cursor_value.projection_epoch != manifest.projection_epoch:
                raise StaleTurnCursorError(
                    session_id=session_id,
                    cursor_epoch=cursor_value.projection_epoch,
                    current_epoch=manifest.projection_epoch,
                )
            anchor = self._files.read_turn_header(
                session_id,
                cursor_value.anchor_turn_id,
                required=True,
            )
            end_offset = (
                anchor.timeline_end
                if cursor_value.include_anchor
                else anchor.timeline_start
            )

        timeline_page = self._timeline.read_visible_entries_before(
            session_id,
            end_offset=end_offset,
            limit=limit,
        )
        items = [
            self._files.read_turn_header(
                session_id,
                entry.turn_id,
                required=True,
            ).summary
            for entry in timeline_page.entries
        ]
        next_cursor = None
        if timeline_page.next_anchor_turn_id is not None:
            next_cursor = self._encode_cursor(
                TurnCursorDTO(
                    session_id=session_id,
                    projection_epoch=manifest.projection_epoch,
                    anchor_turn_id=timeline_page.next_anchor_turn_id,
                )
            )
        return TurnPageDTO(
            items=items,
            next_cursor=next_cursor,
            has_more=timeline_page.has_more,
            projection_epoch=manifest.projection_epoch,
        )

    def _latest_summary_page(
        self,
        session_id: str,
        *,
        manifest: TurnManifest,
        index: TurnIndex,
    ) -> TurnPageDTO:
        item = (
            self._files.read_turn_header(
                session_id,
                index.latest_turn_id,
                required=True,
            ).summary
            if index.latest_turn_id is not None
            else None
        )
        has_more = index.turn_count > 1
        return TurnPageDTO(
            items=[item] if item is not None else [],
            next_cursor=(
                self._encode_cursor(
                    TurnCursorDTO(
                        session_id=session_id,
                        projection_epoch=manifest.projection_epoch,
                        anchor_turn_id=item.turn_id,
                    )
                )
                if item is not None and has_more
                else None
            ),
            has_more=has_more,
            projection_epoch=manifest.projection_epoch,
        )

    @staticmethod
    def to_summary(turn: TurnDetailDTO) -> TurnSummaryDTO:
        return to_turn_summary(turn)

    @staticmethod
    def _encode_cursor(cursor: TurnCursorDTO) -> str:
        raw = cursor.model_dump_json().encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> TurnCursorDTO:
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(cursor + padding)
            return TurnCursorDTO.model_validate_json(raw)
        except Exception as error:
            raise InvalidTurnCursorError("Turn cursor 格式无效") from error
