"""canonical 历史消息的有界正文查询及命中校验；不建立第二份正文索引。"""

import base64
import binascii
from dataclasses import dataclass
from time import monotonic
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.turn_search import (
    TurnMessageLocationDTO,
    TurnMessageMatchDTO,
    TurnMessageReferenceDTO,
    TurnMessageSearchRequest,
    TurnMessageSearchResultsDTO,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.storage.catalog.projections import (
    canonical_projection_content,
)
from app.services.infrastructure.rollout_history.snapshot import IndexedHistory
from app.services.infrastructure.rollout_history.tool_result import (
    ToolOutputChangedError,
    read_tool_result_output,
    tool_result_identity,
)
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.infrastructure.turn_history.models import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)

MAX_SEARCH_ITEMS = 256
MAX_SEARCH_BYTES = 8 * 1024 * 1024
SEARCH_SECONDS = 1.0
PREVIEW_CODEPOINTS = 240


@dataclass
class MessageSearchBudget:
    """多个 Thread 共用一次正文读取预算；单 Thread 入口使用相同上限。"""

    deadline: float
    scanned_items: int = 0
    scanned_bytes: int = 0

    @classmethod
    def create(cls) -> "MessageSearchBudget":
        return cls(deadline=monotonic() + SEARCH_SECONDS)


class _MessageCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    session_id: str
    thread_id: str
    rollout_id: str
    query: str
    category: Literal["message", "tool_result"] = "message"
    view_id: str | None
    projection_epoch: int
    history_view_revision: int
    ordinal: int = Field(default=0, ge=0)
    text_offset: int = Field(default=0, ge=0)


def _cursor(indexed: IndexedHistory, request: TurnMessageSearchRequest) -> _MessageCursor:
    snapshot = indexed.snapshot
    current = _MessageCursor(
        session_id=snapshot.session_id, thread_id=snapshot.thread_id,
        rollout_id=indexed.rollout_id, query=request.query, category=request.category, view_id=indexed.view_id,
        projection_epoch=indexed.projection_epoch,
        history_view_revision=snapshot.manifest.history_view_revision,
    )
    if request.cursor is None:
        return current
    try:
        raw = base64.b64decode(request.cursor + "=" * (-len(request.cursor) % 4), altchars=b"-_", validate=True)
        previous = _MessageCursor.model_validate_json(raw)
    except (ValueError, binascii.Error, ValidationError) as error:
        raise InvalidTurnCursorError("消息搜索 cursor 格式无效") from error
    for field in ("session_id", "thread_id", "rollout_id", "query", "category"):
        if getattr(previous, field) != getattr(current, field):
            raise InvalidTurnCursorError("消息搜索 cursor 不属于当前 owner 或查询")
    if previous.projection_epoch != current.projection_epoch or previous.history_view_revision != current.history_view_revision:
        raise StaleTurnCursorError(
            session_id=snapshot.session_id, cursor_epoch=previous.projection_epoch,
            current_epoch=current.projection_epoch,
            cursor_history_view_revision=previous.history_view_revision,
            current_history_view_revision=current.history_view_revision,
        )
    if previous.view_id != current.view_id:
        raise InvalidTurnCursorError("消息搜索 cursor 不属于当前历史视图")
    return previous


def _searchable_text(item: CanonicalItemRecord) -> str:
    if item.source_manifest is not None and item.source_manifest.protection != "public":
        return ""
    return item.payload if item.semantic_kind == "tool_result" and isinstance(item.payload, str) else canonical_projection_content(item)


def search_messages(
    reader: RolloutContextReader, indexed: IndexedHistory, request: TurnMessageSearchRequest,
    *, budget: MessageSearchBudget | None = None, tool_outputs: ToolOutputStore | None = None,
) -> TurnMessageSearchResultsDTO:
    budget = budget or MessageSearchBudget.create()
    deadline = budget.deadline
    state = _cursor(indexed, request)
    items: list[TurnMessageMatchDTO] = []
    scanned_items = scanned_bytes = 0
    reason = None
    complete = False
    rows: list[tuple[int, str, str, int]] = []
    if indexed.view_id is not None:
        try:
            rows = reader.read_message_search_candidates(
                indexed.snapshot, indexed.chain, lower_ordinal=state.ordinal,
                limit=MAX_SEARCH_ITEMS + 1, deadline=deadline,
                category=request.category,
            )
        except TimeoutError:
            reason = "deadline"
    for ordinal, item_id, turn_id, length in rows:
        if budget.scanned_items >= MAX_SEARCH_ITEMS:
            reason = "scan_limit"
            break
        if monotonic() >= deadline:
            reason = "deadline"
            break
        if length > MAX_SEARCH_BYTES:
            reason = "item_size_limit"
            break
        if budget.scanned_bytes + length > MAX_SEARCH_BYTES:
            reason = "byte_limit"
            break
        # 先按 catalog locator 计费，再沿原 canonical reader 核验并读取正文。
        scanned_items += 1
        scanned_bytes += length
        budget.scanned_items += 1
        budget.scanned_bytes += length
        item, = reader.read_canonical_items(indexed.snapshot, item_ids=[item_id])
        text = _searchable_text(item)
        if request.category == "tool_result" and (item.source_manifest is None or item.source_manifest.protection == "public"):
            output = read_tool_result_output(item, tool_outputs, MAX_SEARCH_BYTES - budget.scanned_bytes)
            if output is not None:
                text, byte_count = output
                if text is None:
                    reason = "item_size_limit" if length + byte_count > MAX_SEARCH_BYTES else "byte_limit"
                    break
                scanned_bytes += byte_count
                budget.scanned_bytes += byte_count
        offset = state.text_offset if ordinal == state.ordinal else 0
        if offset > len(text):
            raise InvalidTurnCursorError("消息搜索 cursor 的正文位置越界")
        state.ordinal, state.text_offset = ordinal, offset
        while offset <= len(text):
            if monotonic() >= deadline:
                reason = "deadline"
                break
            start = text.find(request.query, offset)
            if start < 0:
                break
            end = start + len(request.query)
            preview_start = max(0, start - 40)
            items.append(TurnMessageMatchDTO(
                turn_id=turn_id,
                message_id=item.projection_identity.projection_message_id if item.projection_identity else None,
                role=cast(Literal["user", "assistant", "tool"], "tool" if request.category == "tool_result" else item.wire_role),
                reference=TurnMessageReferenceDTO(
                    item_id=item_id, content_hash=item.content_hash,
                    start_codepoint=start, end_codepoint=end,
                    category=request.category,
                ),
                preview=text[preview_start:preview_start + PREVIEW_CODEPOINTS],
                preview_start_codepoint=preview_start,
                **(tool_result_identity(item) if request.category == "tool_result" else {}),
            ))
            offset = end
            state.text_offset = end
            if len(items) >= request.limit:
                reason = "result_limit"
                break
        if reason is not None:
            break
        state.ordinal, state.text_offset = ordinal + 1, 0
    else:
        complete = reason is None
    return TurnMessageSearchResultsDTO(
        session_id=indexed.snapshot.session_id, thread_id=indexed.snapshot.thread_id,
        items=items, complete=complete, limited_reason=reason,
        cursor=base64.urlsafe_b64encode(state.model_dump_json().encode()).decode().rstrip("=")
        if not complete and reason != "item_size_limit" else None,
        scanned_items=scanned_items, scanned_bytes=scanned_bytes,
    )


def verify_message_reference(
    reader: RolloutContextReader, indexed: IndexedHistory, turn_id: str,
    reference: TurnMessageReferenceDTO,
    *, tool_outputs: ToolOutputStore | None = None,
) -> TurnMessageLocationDTO:
    rows = [] if indexed.view_id is None else reader.read_message_search_candidates(
        indexed.snapshot, indexed.chain, lower_ordinal=0, limit=1,
        deadline=monotonic() + SEARCH_SECONDS, item_id=reference.item_id,
        category=reference.category,
    )
    if rows and rows[0][2] == turn_id and rows[0][3] <= MAX_SEARCH_BYTES:
        item, = reader.read_canonical_items(indexed.snapshot, item_ids=[reference.item_id])
        if item.content_hash != reference.content_hash:
            raise StaleTurnReferenceError(session_id=indexed.snapshot.session_id, turn_ids=[turn_id])
        text = _searchable_text(item)
        if reference.category == "tool_result" and (item.source_manifest is None or item.source_manifest.protection == "public"):
            try:
                output = read_tool_result_output(item, tool_outputs, MAX_SEARCH_BYTES - rows[0][3])
            except (FileNotFoundError, ToolOutputChangedError) as error:
                raise StaleTurnReferenceError(session_id=indexed.snapshot.session_id, turn_ids=[turn_id]) from error
            if output is not None:
                text, _ = output
                if text is None:
                    raise StaleTurnReferenceError(session_id=indexed.snapshot.session_id, turn_ids=[turn_id])
        if reference.end_codepoint <= len(text):
            return TurnMessageLocationDTO(
                turn_id=turn_id, message_id=item.projection_identity.projection_message_id if item.projection_identity else None,
                role=cast(Literal["user", "assistant", "tool"], "tool" if reference.category == "tool_result" else item.wire_role),
                reference=reference, content=text,
                **(tool_result_identity(item) if reference.category == "tool_result" else {}),
            )
    raise StaleTurnReferenceError(session_id=indexed.snapshot.session_id, turn_ids=[turn_id])
