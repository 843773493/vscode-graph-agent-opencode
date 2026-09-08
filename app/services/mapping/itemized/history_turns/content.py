"""有序历史 content 纯投影，不执行 I/O。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, BaseMessage

from app.schemas.internal_v2.turn import (
    TurnAttachmentDTO,
    TurnThinkingBlockDTO,
    TurnUserMessageDTO,
)
from app.services.mapping.itemized.provider_history import reasoning_projection_rows
from app.services.mapping.itemized.provider_history import (
    visible_text as litellm_visible_text,
)
from app.services.mapping.user_message_content_projection import user_content_projection


def nonnegative_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def duration_milliseconds(start: object, end: object) -> int | None:
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


def message_id(message: BaseMessage, fallback: str) -> str:
    if isinstance(message.id, str) and message.id:
        return message.id
    raw = message.response_metadata.get("message_id")
    return raw if isinstance(raw, str) and raw else fallback


def message_time(
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
    if fallback is None:
        raise ValueError("历史消息缺少已提交的 created_at")
    return fallback


def projection_time(projection: Mapping[str, object], field: str) -> datetime:
    value = projection.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"历史索引缺少 {field}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"历史索引 {field} 缺少时区")
    return parsed


def content_text(message: BaseMessage | None) -> str:
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def visible_content_text(message: BaseMessage | None) -> str:
    if message is None:
        return ""
    return litellm_visible_text(message.content)


def thinking_blocks(messages: list[BaseMessage]) -> list[TurnThinkingBlockDTO]:
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


def is_internal(message: BaseMessage) -> bool:
    metadata = message.response_metadata
    if metadata.get("internal") is True:
        return True
    message_metadata = metadata.get("message_metadata")
    return (
        isinstance(message_metadata, Mapping)
        and message_metadata.get("internal") is True
    )


def final_message(
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
            if not isinstance(message, AIMessage) or is_internal(message):
                raise RuntimeError(
                    "SQLite final_message_sequence 未指向可见 AIMessage: "
                    f"sequence={final_sequence}"
                )
            if isinstance(final_message_id, str) and final_message_id:
                actual_id = message_id(message, "")
                if actual_id != final_message_id:
                    raise RuntimeError(
                        "SQLite finalization 指针与消息 ID 不一致: "
                        f"sequence={final_sequence} expected={final_message_id} actual={actual_id}"
                    )
            return message
        raise RuntimeError(
            "SQLite final_message_sequence 未指向当前可见消息: "
            f"sequence={final_sequence}"
        )
    if isinstance(final_message_id, str) and final_message_id:
        for message in messages:
            if (
                isinstance(message, AIMessage)
                and message_id(message, "") == final_message_id
                and not is_internal(message)
            ):
                return message
        raise RuntimeError(
            "SQLite final_message_id 未指向当前可见 AIMessage: "
            f"message_id={final_message_id}"
        )
    return None


def user_message(
    session_id: str,
    turn_id: str,
    message: BaseMessage,
    index: int,
) -> TurnUserMessageDTO:
    response_metadata = dict(message.response_metadata)
    projection = user_content_projection(message.content, response_metadata)
    attachments = [
        TurnAttachmentDTO.model_validate(
            {str(key): value for key, value in item.items() if str(key) != "data_url"}
        )
        for item in projection.attachments
    ]
    response_metadata.pop("display_content", None)
    response_metadata.pop("attachments", None)
    return TurnUserMessageDTO(
        message_id=message_id(message, f"{turn_id}:user:{index}"),
        content=projection.visible_text,
        content_truncated=False,
        attachments=attachments,
        metadata=response_metadata,
        created_at=message_time(message),
    )
