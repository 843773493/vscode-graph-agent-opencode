"""原历史页内上下文元数据的有界续读，不读取 Turn 或来源正文。"""

import base64

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.internal_v2.turn import ContextNoticeAnchorDTO, TurnResponseItemDTO
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    ContextChain,
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_history.errors import (
    HistoryContextUnavailableError,
    InvalidTurnCursorError,
    StaleTurnCursorError,
)
from app.services.mapping.itemized.history_turns.context_notices import (
    context_notice_parts,
)


class ContextNoticeCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    thread_id: str
    checkpoint_ns: str
    rollout_id: str
    view_id: str
    projection_epoch: int = Field(ge=1)
    history_view_revision: int = Field(ge=0)
    turn_ids: list[str] = Field(max_length=768)
    execution_ids: list[str] = Field(max_length=256)
    call_upper_bound: int = Field(ge=1)
    after_call: int = Field(ge=1)
    after_plan: int = Field(ge=0)
    anchor_context: ContextNoticeAnchorDTO | None = None


def decode_context_cursor(token: str, snapshot: RolloutReadSnapshot, view_id: str | None) -> ContextNoticeCursor:
    try:
        cursor = ContextNoticeCursor.model_validate_json(base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
    except ValueError as error:
        raise InvalidTurnCursorError("上下文元数据游标格式无效") from error
    if (cursor.session_id, cursor.thread_id, cursor.checkpoint_ns, cursor.rollout_id) != (
        snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns, snapshot.rollout_id,
    ):
        raise InvalidTurnCursorError("上下文元数据游标不属于请求的历史 owner")
    if cursor.projection_epoch != snapshot.manifest.projection_epoch:
        raise StaleTurnCursorError(session_id=snapshot.session_id,
            cursor_epoch=cursor.projection_epoch, current_epoch=snapshot.manifest.projection_epoch)
    if cursor.view_id != view_id or cursor.history_view_revision != snapshot.manifest.history_view_revision:
        raise StaleTurnCursorError(session_id=snapshot.session_id,
            cursor_epoch=cursor.projection_epoch, current_epoch=snapshot.manifest.projection_epoch,
            cursor_history_view_revision=cursor.history_view_revision,
            current_history_view_revision=snapshot.manifest.history_view_revision)
    return cursor


def read_notice_parts(reader: RolloutContextReader, snapshot: RolloutReadSnapshot, chain: ContextChain, *,
                      turn_ids: list[str], execution_ids: list[str],
                      cursor: ContextNoticeCursor | None = None,
                      anchor_context: ContextNoticeAnchorDTO | None = None) -> dict[str, list[TurnResponseItemDTO]]:
    anchor_context = cursor.anchor_context if cursor else anchor_context
    rows = reader.read_context_notices(snapshot, chain, turn_ids=turn_ids, execution_ids=execution_ids,
        after=(cursor.after_call, cursor.after_plan) if cursor else (0, -1),
        call_upper_bound=cursor.call_upper_bound if cursor else None,
        model_call_id=anchor_context.model_call_id if anchor_context else None,
        anchor=anchor_context.anchor if anchor_context else None)
    if anchor_context and cursor is None and not rows:
        raise HistoryContextUnavailableError(session_id=snapshot.session_id, thread_id=snapshot.thread_id,
            model_call_id=anchor_context.model_call_id, anchor=anchor_context.anchor)
    continuation = None
    if rows and rows[-1].truncated:
        last = rows[-1]
        next_cursor = ContextNoticeCursor(session_id=snapshot.session_id, thread_id=snapshot.thread_id,
            checkpoint_ns=snapshot.checkpoint_ns, rollout_id=snapshot.rollout_id,
            view_id=reader.context_view_id(chain), projection_epoch=snapshot.manifest.projection_epoch,
            history_view_revision=snapshot.manifest.history_view_revision,
            turn_ids=turn_ids, execution_ids=execution_ids, call_upper_bound=last.call_upper_bound,
            after_call=last.call_order, after_plan=last.plan_ordinal, anchor_context=anchor_context)
        continuation = base64.urlsafe_b64encode(next_cursor.model_dump_json().encode()).decode().rstrip("=")
    return context_notice_parts(rows, continuation=continuation)
