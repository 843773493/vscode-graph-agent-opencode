"""会话上下文分页的游标绑定、字符预算与读取分片。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from typing import Protocol, TypeVar

from app.abstractions.session_context import (
    SessionContextCursorError,
    SessionContextRevisionChangedError,
)
from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextPartialErrorDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceIdentity,
)


def session_context_read_operation(
    *,
    view: str,
    include: Sequence[str],
    recent_rounds: int,
    include_initial_goal: bool,
) -> str:
    selection = json.dumps(
        {
            "include": sorted(set(include)),
            "recent_rounds": recent_rounds,
            "include_initial_goal": include_initial_goal,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"read:{view}:{hashlib.sha256(selection).hexdigest()}"


def require_session_context_revision(expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise SessionContextRevisionChangedError(
            expected_revision=expected,
            actual_revision=actual,
        )


def require_cursor_offset_in_range(offset: int, total: int, *, uri: str) -> None:
    """游标 offset 越过真实条目数时 fail-closed。"""
    if offset > total:
        raise SessionContextCursorError(
            "position_out_of_bounds", f"cursor offset 越界: uri={uri} offset={offset} total={total}"
        )


class _SerializedResult(Protocol):
    returned_chars: int

    def model_dump_json(self) -> str: ...


_SerializedResultT = TypeVar("_SerializedResultT", bound=_SerializedResult)


def set_exact_returned_chars(
    result: _SerializedResultT,
    *,
    operation: str,
) -> _SerializedResultT:
    """把 result.returned_chars 收敛为序列化后的真实字符数。"""
    result.returned_chars = exact_json_result_chars(
        serialized_chars=len(result.model_dump_json()),
        reported_chars=result.returned_chars,
        operation=operation,
    )
    return result


def exact_json_result_chars(
    *,
    serialized_chars: int,
    reported_chars: int,
    operation: str = "session context",
) -> int:
    """returned_chars 只改变自身的十进制位数；完整响应与增量预算共用这次收敛。"""
    base_returned_chars = serialized_chars - len(str(reported_chars))
    candidate_returned_chars = serialized_chars
    for _ in range(8):
        exact_returned_chars = base_returned_chars + len(str(candidate_returned_chars))
        if exact_returned_chars == candidate_returned_chars:
            return exact_returned_chars
        candidate_returned_chars = exact_returned_chars
    raise RuntimeError(f"无法稳定计算 {operation} 响应字符数")


def encode_session_context_cursor(
    *,
    resource_identity: ResourceIdentity,
    uri: str,
    scope: str,
    revision: str,
    operation: str,
    record_index: int | None = None,
    assembly_ref: ResourceIdentity | None = None,
    offset: int,
    char_offset: int = 0,
) -> str:
    payload = json.dumps(
        {
            "resource_identity": str(resource_identity),
            "uri": uri,
            "scope": scope,
            "revision": revision,
            "operation": operation,
            "record_index": record_index,
            "assembly_ref": str(assembly_ref) if assembly_ref is not None else None,
            "offset": offset,
            "char_offset": char_offset,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_session_context_cursor(
    cursor: str | None,
    *,
    resource_identity: ResourceIdentity,
    uri: str,
    scope: str,
    revision: str,
    operation: str,
    record_index: int | None = None,
    assembly_ref: ResourceIdentity | None = None,
) -> tuple[int, int]:
    if cursor is None:
        return 0, 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise SessionContextCursorError("malformed", "cursor 格式无效") from error
    if not isinstance(payload, dict):
        raise SessionContextCursorError("malformed", "cursor payload 必须是对象")
    cursor_revision = payload.get("revision")
    if cursor_revision != revision:
        raise SessionContextRevisionChangedError(
            expected_revision=str(cursor_revision),
            actual_revision=revision,
        )
    expected = {
        "resource_identity": str(resource_identity),
        "uri": uri,
        "scope": scope,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise SessionContextCursorError("owner_mismatch", "cursor 与当前资源或查询选择不匹配")
    selection = {"operation": operation, "record_index": record_index,
                 "assembly_ref": str(assembly_ref) if assembly_ref is not None else None}
    if any(payload.get(key) != value for key, value in selection.items()):
        raise SessionContextCursorError("query_mismatch", "cursor 与当前资源或查询选择不匹配")
    offset = payload.get("offset")
    if type(offset) is not int or offset < 0:
        raise SessionContextCursorError("position_invalid", "cursor offset 无效")
    char_offset = payload.get("char_offset", 0)
    if type(char_offset) is not int or char_offset < 0:
        raise SessionContextCursorError("position_invalid", "cursor char_offset 无效")
    return offset, char_offset



def paginate_read_items(
    *,
    request: SessionContextReadRequest,
    revision: str,
    items: list[SessionContextItemDTO],
    offset: int,
    char_offset: int,
    compacted: bool = False,
    compaction_cutoff: int | None = None,
    raw_message_count: int | None = 0,
    effective_record_count: int = 0,
) -> SessionContextReadResultDTO:
    require_cursor_offset_in_range(offset, len(items), uri=request.uri)
    if offset == len(items) and char_offset:
        raise SessionContextCursorError("position_out_of_bounds", "context cursor 已到末尾，不能继续读取 item 内字符")
    selected = items[offset:offset + request.limit]
    returned: list[SessionContextItemDTO] = []
    budget_truncated = False
    completed_items = 0
    returned_item_json_chars = 0
    next_item_char_offset: int | None = None
    for selected_index, item in enumerate(selected):
        item_char_offset = char_offset if selected_index == 0 else 0
        serialized_item: str | None = None
        if item_char_offset == 0:
            serialized_item = item.model_dump_json()
            candidate_completed = completed_items + 1
            empty_candidate = _build_read_result(
                request=request,
                revision=revision,
                items=[],
                offset=offset,
                completed_items=candidate_completed,
                total_items=len(items),
                next_item_char_offset=None,
                compacted=compacted,
                compaction_cutoff=compaction_cutoff,
                raw_message_count=raw_message_count,
                effective_record_count=effective_record_count,
                budget_truncated=budget_truncated,
            )
            candidate_returned_chars = exact_json_result_chars(
                serialized_chars=(
                    empty_candidate.returned_chars + returned_item_json_chars + len(serialized_item) + len(returned)
                ),
                reported_chars=empty_candidate.returned_chars,
            )
            if candidate_returned_chars <= request.max_chars:
                returned.append(item)
                returned_item_json_chars += len(serialized_item)
                completed_items = candidate_completed
                continue
        if returned:
            budget_truncated = True
            break
        if serialized_item is None:
            serialized_item = item.model_dump_json()
        fitted, remaining_item_offset = _largest_fitting_chunk(
            request=request,
            revision=revision,
            item=item,
            serialized=serialized_item,
            item_offset=offset,
            total_items=len(items),
            char_offset=item_char_offset,
            compacted=compacted,
            compaction_cutoff=compaction_cutoff,
            raw_message_count=raw_message_count,
            effective_record_count=effective_record_count,
        )
        if fitted is None:
            raise ValueError("max_chars 太小，无法返回包含资源身份的最小上下文分片")
        returned.append(fitted)
        if remaining_item_offset is not None:
            budget_truncated = True
            next_item_char_offset = remaining_item_offset
            break
        completed_items += 1
        returned_item_json_chars += len(fitted.model_dump_json())
    result = _build_read_result(
        request=request,
        revision=revision,
        items=returned,
        offset=offset,
        completed_items=completed_items,
        total_items=len(items),
        next_item_char_offset=next_item_char_offset,
        compacted=compacted,
        compaction_cutoff=compaction_cutoff,
        raw_message_count=raw_message_count,
        effective_record_count=effective_record_count,
        budget_truncated=budget_truncated,
    )
    if result.returned_chars > request.max_chars:
        raise RuntimeError("上下文分页器生成了超过 max_chars 的响应")
    return result


def _largest_fitting_chunk(
    *,
    request: SessionContextReadRequest,
    revision: str,
    item: SessionContextItemDTO,
    serialized: str,
    item_offset: int,
    total_items: int,
    char_offset: int,
    compacted: bool,
    compaction_cutoff: int | None,
    raw_message_count: int | None,
    effective_record_count: int,
) -> tuple[SessionContextItemDTO | None, int | None]:
    # 分片必须与完整DTO同形，保留nullable来源URI与空集合，消费者无需补字段。
    if char_offset >= len(serialized):
        raise SessionContextCursorError(
            "position_out_of_bounds", f"context cursor 字符偏移越界: offset={char_offset}, "
            f"total={len(serialized)}"
        )
    low = char_offset + 1
    # JSON 响应不会短于分片正文，无需序列化超过响应预算的候选片段。
    high = min(len(serialized), char_offset + request.max_chars)
    best: tuple[SessionContextItemDTO, int | None] | None = None
    while low <= high:
        end = (low + high) // 2
        remaining_offset = end if end < len(serialized) else None
        chunk = _chunk_item(item, serialized, char_offset, end)
        result = _build_read_result(
            request=request,
            revision=revision,
            items=[chunk],
            offset=item_offset,
            completed_items=0 if remaining_offset is not None else 1,
            total_items=total_items,
            next_item_char_offset=remaining_offset,
            compacted=compacted,
            compaction_cutoff=compaction_cutoff,
            raw_message_count=raw_message_count,
            effective_record_count=effective_record_count,
            budget_truncated=remaining_offset is not None,
        )
        if result.returned_chars <= request.max_chars:
            best = chunk, remaining_offset
            low = end + 1
        else:
            high = end - 1
    return best if best is not None else (None, None)


def _chunk_item(
    item: SessionContextItemDTO,
    serialized: str,
    start: int,
    end: int,
) -> SessionContextItemDTO:
    has_more = end < len(serialized)
    data: dict[str, object] = {"chunk_start": start}
    if has_more:
        data.update({"next_chunk_start": end, "total_chars": len(serialized)})
    return SessionContextItemDTO(
        kind=f"{item.kind}_chunk",
        resource_identity=item.resource_identity,
        uri=item.uri,
        scope=item.scope,
        assembly_ref=item.assembly_ref,
        role=item.role,
        record_index=item.record_index,
        text=serialized[start:end],
        data=data,
        truncated=has_more,
    )


def _build_read_result(
    *,
    request: SessionContextReadRequest,
    revision: str,
    items: list[SessionContextItemDTO],
    offset: int,
    completed_items: int,
    total_items: int,
    next_item_char_offset: int | None,
    compacted: bool,
    compaction_cutoff: int | None,
    raw_message_count: int | None,
    effective_record_count: int,
    budget_truncated: bool,
) -> SessionContextReadResultDTO:
    has_more = next_item_char_offset is not None or offset + completed_items < total_items
    next_cursor = None
    if has_more:
        next_cursor = encode_session_context_cursor(
            resource_identity=request.resource_identity,
            uri=request.uri,
            scope=request.scope,
            revision=revision,
            operation=session_context_read_operation(
                view=request.view,
                include=request.include,
                recent_rounds=request.recent_rounds,
                include_initial_goal=request.include_initial_goal,
            ),
            record_index=request.record_index,
            assembly_ref=request.assembly_ref,
            offset=offset + completed_items,
            char_offset=next_item_char_offset or 0,
        )
    result = SessionContextReadResultDTO(
        resource_identity=request.resource_identity,
        uri=request.uri,
        scope=request.scope,
        view=request.view,
        record_index=request.record_index,
        assembly_ref=request.assembly_ref,
        revision=revision,
        compacted=compacted,
        compaction_cutoff=compaction_cutoff,
        raw_message_count=raw_message_count,
        effective_record_count=effective_record_count,
        truncated=budget_truncated or has_more,
        has_more=has_more,
        next_cursor=next_cursor,
        items=items,
        partial_errors=[SessionContextPartialErrorDTO(
            resource_identity=request.resource_identity,
            error="detail-unavailable: 压缩前原消息组未保留，无法取得准确的原消息数量；当前上下文仍可读取。",
        )] if raw_message_count is None else [],
    )
    return set_exact_returned_chars(result, operation="read_context")
