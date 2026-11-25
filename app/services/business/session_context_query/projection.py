from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict

from app.abstractions.session_context import (
    AgentContextState,
)
from app.prompting.validation import internal_prompt_metadata
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_context import (
    SessionContextInclude,
    SessionContextItemDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
)
from app.services.business.session_context_query.pagination import (
    decode_session_context_cursor,
    paginate_read_items,
    require_session_context_revision,
    session_context_read_operation,
)
from app.services.mapping.itemized.provider_history import (
    public_message_record,
    reasoning_projection_rows,
)
from app.services.mapping.itemized.provider_history import (
    visible_text as provider_visible_text,
)


class SessionContextReadSnapshot(BaseModel):
    """冻结的完整冷投影；普通 read 和 durable RemoteObservation 共用分页器。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str
    items: list[SessionContextItemDTO]
    compacted: bool = False
    compaction_cutoff: int | None = None
    raw_message_count: int | None = 0
    effective_record_count: int = 0

    def page(self, request: SessionContextReadRequest) -> SessionContextReadResultDTO:
        require_session_context_revision(request.revision, self.revision)
        offset, char_offset = decode_session_context_cursor(
            request.cursor, resource_identity=request.resource_identity, uri=request.uri, scope=request.scope,
            revision=self.revision, operation=session_context_read_operation(
                view=request.view, include=request.include, recent_rounds=request.recent_rounds,
                include_initial_goal=request.include_initial_goal,
            ), record_index=request.record_index, assembly_ref=request.assembly_ref,
        )
        return paginate_read_items(
            request=request, revision=self.revision, items=self.items, offset=offset, char_offset=char_offset,
            compacted=self.compacted, compaction_cutoff=self.compaction_cutoff,
            raw_message_count=self.raw_message_count, effective_record_count=self.effective_record_count,
        )


def public_session_data(session: SessionDTO) -> dict[str, object]:
    data = session.model_dump(mode="json")
    allowed = {
        "session_id",
        "workspace_id",
        "title",
        "current_agent_id",
        "current_model",
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


def context_revision(state: AgentContextState) -> str:
    # checkpoint_id 只用于 Saver 定位；revision 必须反映查询实际看到的内容。
    payload = {
        "records": state["records"],
        "raw_message_count": state["raw_message_count"],
        "compacted": state["compacted"],
        "compaction_cutoff": state["compaction_cutoff"],
    }
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # 逐段摘要，避免把全部历史再合并成一份字符串和 bytes。
    for chunk in encoder.iterencode(payload):
        digest.update(chunk.encode("utf-8"))
    return f"content:{digest.hexdigest()}"


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
    projected = provider_visible_text(content)
    if projected:
        return projected.strip()
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
    response_metadata = record.get("response_metadata")
    is_internal = (
        internal_prompt_metadata(response_metadata)
        if isinstance(response_metadata, dict)
        else None
    )
    return (
        record_role(record) == "user"
        and bool(text)
        and is_internal is None
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
    resource_identity: str,
    uri: str,
    scope: str,
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
                resource_identity=resource_identity,
                uri=uri,
                scope=scope,
                role=role,
                record_index=index,
                text=text or None,
                reasoning=reasoning or None,
                tool_summary=summary,
                tool_calls=calls,
                tool_results=results,
                raw_record=public_message_record(record) if "raw_record" in include else None,
            )
        )
    return items


def _content_blocks(record: dict[str, object]) -> list[dict[str, object]]:
    content = record.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _reasoning_text(record: dict[str, object]) -> str:
    return "\n".join(
        str(row["text"])
        for row in reasoning_projection_rows(record.get("content"))
        if row.get("kind") in {"reasoning", "summary"}
        and isinstance(row.get("text"), str)
        and row["text"]
    )


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
