"""将原生消息或字典投影为 Agent State 记录，保留恢复所需的原始块。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage

from app.services.mapping.message_content import (
    extract_message_content,
    json_message_value,
    message_role,
)


def project_agent_state_records(messages: Sequence[object], *, context_name: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, BaseMessage):
            records.append(message_to_agent_state_record(message))
        elif isinstance(message, Mapping):
            records.append(_mapping_to_agent_state_record(message))
        else:
            raise TypeError(
                f"{context_name} messages 中出现不支持的消息类型: {type(message).__name__}"
            )
    return _dedupe_consecutive_agent_state_records(records)


def message_to_agent_state_record(message: BaseMessage) -> dict[str, object]:
    extracted = extract_message_content(message)
    raw_content = getattr(message, "content", "")
    record: dict[str, object] = {
        "role": message_role(message).value,
        "type": message.type,
        # 原始记录保留原生布局，轻量展示不能成为恢复正文的替代品。
        "content": json_message_value(raw_content),
    }
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        record["tool_calls"] = json_message_value(tool_calls)

    tool_call_id = getattr(message, "tool_call_id", None)
    if isinstance(tool_call_id, str) and tool_call_id:
        record["tool_call_id"] = tool_call_id

    name = getattr(message, "name", None)
    if isinstance(name, str) and name:
        record["name"] = name

    response_metadata = dict(message.response_metadata or {})
    response_metadata.pop("display_content", None)
    phase = response_metadata.get("phase")
    if not isinstance(phase, str) and isinstance(message, AIMessage):
        content = extracted["content"]
        if tool_calls:
            response_metadata["phase"] = "commentary"
        elif isinstance(content, str) and content:
            response_metadata["phase"] = "final_answer"
    if response_metadata:
        record["response_metadata"] = json_message_value(response_metadata)

    usage_metadata = getattr(message, "usage_metadata", None)
    if usage_metadata:
        record["usage_metadata"] = json_message_value(usage_metadata)

    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    if additional_kwargs:
        record["additional_kwargs"] = json_message_value(additional_kwargs)

    return record


def _mapping_to_agent_state_record(
    message: Mapping[object, object],
) -> dict[str, object]:
    allowed_keys = (
        "role",
        "type",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "response_metadata",
        "usage_metadata",
        "additional_kwargs",
    )
    record: dict[str, object] = {}
    for key in allowed_keys:
        if key not in message:
            continue
        value = message[key]
        if value is None or value == "" or value == []:
            continue
        record[key] = json_message_value(value)
    if record:
        return record

    ignored_keys = {
        "additional_kwargs",
        "id",
        "metadata",
        "response_metadata",
        "usage_metadata",
    }
    return {
        str(key): json_message_value(value)
        for key, value in message.items()
        if str(key) not in ignored_keys
    }


def _agent_state_record_key(record: Mapping[str, object]) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dedupe_consecutive_agent_state_records(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    previous_key: str | None = None
    for record in records:
        current_key = _agent_state_record_key(record)
        if current_key == previous_key:
            continue
        deduped.append(record)
        previous_key = current_key
    return deduped
