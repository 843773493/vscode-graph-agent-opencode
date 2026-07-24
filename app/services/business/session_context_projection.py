from __future__ import annotations

import hashlib
import json

from app.schemas.public_v2.session import SessionDTO
from app.schemas.public_v2.session_context import (
    SessionContextInclude,
    SessionContextItemDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
)
from app.services.business.session_context_resource import SessionContextCursorCodec


def public_session_data(session: SessionDTO) -> dict[str, object]:
    data = session.model_dump(mode="json")
    allowed = {
        "session_id",
        "workspace_id",
        "title",
        "current_agent_id",
        "current_provider_id",
        "parent_session_id",
        "kind",
        "created_at",
        "updated_at",
    }
    return {key: value for key, value in data.items() if key in allowed}


def sessions_revision(sessions: list[SessionDTO]) -> str:
    payload = [session.model_dump(mode="json") for session in sessions]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def record_role(record: dict[str, object]) -> str | None:
    role = record.get("role")
    if isinstance(role, str):
        return role
    message_type = record.get("type")
    mapping = {"human": "user", "ai": "assistant", "tool": "tool"}
    return mapping.get(message_type) if isinstance(message_type, str) else None


def visible_text(record: dict[str, object]) -> str:
    if record_role(record) == "tool" or record.get("tool_call_id") is not None:
        return ""
    content = record.get("content")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in _content_blocks(record):
        if block.get("type") not in {None, "text", "input_text", "output_text"}:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def is_effective_user(record: dict[str, object]) -> bool:
    text = visible_text(record)
    return (
        record_role(record) == "user"
        and bool(text)
        and not text.lstrip().startswith("<system_reminder>")
    )


def tool_summary(record: dict[str, object]) -> list[str]:
    summaries: list[str] = []
    for call in _tool_calls(record):
        name = call.get("name")
        if isinstance(name, str):
            summaries.append(name)
    if record_role(record) == "tool":
        name = record.get("name")
        summaries.append(str(name) if name else "tool result")
    return summaries


def project_record_items(
    *,
    resource: str,
    records: list[dict[str, object]],
    include: set[SessionContextInclude],
    messages_only: bool,
    indexes: list[int] | None = None,
) -> list[SessionContextItemDTO]:
    target_indexes = indexes if indexes is not None else list(range(len(records)))
    items: list[SessionContextItemDTO] = []
    for index in target_indexes:
        record = records[index]
        role = record_role(record)
        if role in {"system", "developer"} and "system" not in include:
            continue
        text = visible_text(record) if "visible_text" in include else ""
        reasoning = _reasoning_text(record) if "reasoning" in include else ""
        calls = _tool_calls(record) if "tool_calls" in include else []
        results = _tool_results(record) if "tool_results" in include else []
        summary = tool_summary(record) if "tool_summary" in include else []
        if messages_only and not any((text, reasoning, calls, results, summary)):
            continue
        items.append(
            SessionContextItemDTO(
                kind="message" if messages_only else "record",
                locator=f"{resource}#record={index}",
                role=role,
                record_index=index,
                text=text or None,
                reasoning=reasoning or None,
                tool_summary=summary,
                tool_calls=calls,
                tool_results=results,
                raw_record=record if "raw_record" in include else None,
            )
        )
    return items


def paginate_read_items(
    *,
    request: SessionContextReadRequest,
    resource: str,
    revision: str,
    items: list[SessionContextItemDTO],
    offset: int,
    char_offset: int,
    compacted: bool = False,
    compaction_cutoff: int | None = None,
    raw_message_count: int = 0,
    effective_record_count: int = 0,
) -> SessionContextReadResultDTO:
    selected = items[offset:offset + request.limit]
    returned: list[SessionContextItemDTO] = []
    budget_truncated = False
    completed_items = 0
    next_item_char_offset: int | None = None
    for selected_index, item in enumerate(selected):
        item_char_offset = char_offset if selected_index == 0 else 0
        if item_char_offset == 0:
            candidate_items = [*returned, item]
            candidate_completed = completed_items + 1
            candidate = _build_read_result(
                request=request,
                resource=resource,
                revision=revision,
                items=candidate_items,
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
            if candidate.returned_chars <= request.max_chars:
                returned = candidate_items
                completed_items = candidate_completed
                continue
        if returned:
            budget_truncated = True
            break
        fitted, remaining_item_offset = _largest_fitting_chunk(
            request=request,
            resource=resource,
            revision=revision,
            item=item,
            item_offset=offset,
            total_items=len(items),
            char_offset=item_char_offset,
            compacted=compacted,
            compaction_cutoff=compaction_cutoff,
            raw_message_count=raw_message_count,
            effective_record_count=effective_record_count,
        )
        if fitted is None:
            raise ValueError("max_chars 太小，无法返回包含 locator 的最小上下文分片")
        returned.append(fitted)
        if remaining_item_offset is not None:
            budget_truncated = True
            next_item_char_offset = remaining_item_offset
            break
        completed_items += 1
    result = _build_read_result(
        request=request,
        resource=resource,
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
    resource: str,
    revision: str,
    item: SessionContextItemDTO,
    item_offset: int,
    total_items: int,
    char_offset: int,
    compacted: bool,
    compaction_cutoff: int | None,
    raw_message_count: int,
    effective_record_count: int,
) -> tuple[SessionContextItemDTO | None, int | None]:
    serialized = item.model_dump_json(exclude_none=True, exclude_defaults=True)
    if char_offset >= len(serialized):
        raise ValueError(
            f"context cursor 字符偏移越界: offset={char_offset}, "
            f"total={len(serialized)}"
        )
    low = char_offset + 1
    high = len(serialized)
    best: tuple[SessionContextItemDTO, int | None] | None = None
    while low <= high:
        end = (low + high) // 2
        remaining_offset = end if end < len(serialized) else None
        chunk = _chunk_item(item, serialized, char_offset, end)
        result = _build_read_result(
            request=request,
            resource=resource,
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
        locator=item.locator,
        role=item.role,
        record_index=item.record_index,
        text=serialized[start:end],
        data=data,
        truncated=has_more,
    )


def _build_read_result(
    *,
    request: SessionContextReadRequest,
    resource: str,
    revision: str,
    items: list[SessionContextItemDTO],
    offset: int,
    completed_items: int,
    total_items: int,
    next_item_char_offset: int | None,
    compacted: bool,
    compaction_cutoff: int | None,
    raw_message_count: int,
    effective_record_count: int,
    budget_truncated: bool,
) -> SessionContextReadResultDTO:
    has_more = next_item_char_offset is not None or offset + completed_items < total_items
    next_cursor = None
    if has_more:
        next_cursor = SessionContextCursorCodec.encode(
            resource=resource,
            revision=revision,
            operation=f"read:{request.view}",
            offset=offset + completed_items,
            char_offset=next_item_char_offset or 0,
        )
    result = SessionContextReadResultDTO(
        resource=resource,
        view=request.view,
        revision=revision,
        compacted=compacted,
        compaction_cutoff=compaction_cutoff,
        raw_message_count=raw_message_count,
        effective_record_count=effective_record_count,
        truncated=budget_truncated or has_more,
        has_more=has_more,
        next_cursor=next_cursor,
        items=items,
    )
    return _set_exact_returned_chars(result)


def _set_exact_returned_chars(
    result: SessionContextReadResultDTO,
) -> SessionContextReadResultDTO:
    for _ in range(8):
        length = len(result.model_dump_json())
        if result.returned_chars == length:
            return result
        result.returned_chars = length
    raise RuntimeError("无法稳定计算 read_context 响应字符数")


def _content_blocks(record: dict[str, object]) -> list[dict[str, object]]:
    content = record.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _reasoning_text(record: dict[str, object]) -> str:
    parts: list[str] = []
    top_level = record.get("reasoning_content")
    if isinstance(top_level, str) and top_level.strip():
        parts.append(top_level.strip())
    for block in _content_blocks(record):
        if block.get("type") != "reasoning":
            continue
        for key in ("reasoning", "text", "summary"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
    return "\n".join(parts)


def _tool_calls(record: dict[str, object]) -> list[dict[str, object]]:
    value = record.get("tool_calls")
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _tool_results(record: dict[str, object]) -> list[dict[str, object]]:
    if record_role(record) != "tool" and record.get("tool_call_id") is None:
        return []
    result: dict[str, object] = {}
    for key in ("tool_call_id", "name", "content"):
        if key in record:
            result[key] = record[key]
    return [result] if result else []
