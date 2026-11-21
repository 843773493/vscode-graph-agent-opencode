"""消息正文、附件与可见 DTO 的无 I/O 映射，供业务服务显式调用。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.prompting.validation import internal_prompt_metadata
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import AttachmentRef, MessageDTO
from app.services.business.message_display import project_message_for_display
from app.services.mapping.agent_content_mapper import split_agent_content
from app.services.mapping.user_message_content_projection import (
    UserContentProjection,
    user_content_projection,
)


def checkpoint_message_to_dto(
    session_id: str,
    thread_id: str,
    index: int,
    message: BaseMessage,
) -> MessageDTO:
    role = message_role(message)
    extracted = extract_message_content(message)
    content = extracted["content"]
    response_metadata = message.response_metadata or {}
    structured_metadata = internal_prompt_metadata(response_metadata)
    user_projection: UserContentProjection | None = None
    display_projection = None
    if isinstance(message.content, str):
        if structured_metadata is not None or role != MessageRole.user:
            display_projection = project_message_for_display(
                message.content,
                response_metadata,
            )
            content = display_projection.content
    elif structured_metadata is not None:
        raise TypeError("内部结构消息 content 必须是字符串")
    else:
        if role == MessageRole.user:
            user_projection = user_content_projection(
                message.content,
                response_metadata,
            )
            content = user_projection.visible_text
        else:
            display_content = response_metadata.get("display_content")
            if display_content is not None:
                if not isinstance(display_content, str):
                    raise TypeError("message metadata.display_content 必须是字符串")
                content = display_content
    message_id = response_metadata.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise RuntimeError(
            "用户可见消息缺少持久化 message_id: "
            f"checkpoint_index={index} message_type={message.type}"
        )
    created_at = _metadata_datetime(response_metadata, "created_at")
    updated_at = _metadata_datetime(response_metadata, "updated_at")
    metadata: dict[str, object] = {
        "langchain_type": message.type,
        "tool_calls": getattr(message, "tool_calls", None) or [],
        "tool_call_id": getattr(message, "tool_call_id", None),
    }
    display_blocks = _display_content_blocks(
        extracted["content_blocks"]
    )
    if display_blocks:
        metadata["content_blocks"] = display_blocks
    if extracted["reasoning_id"] is not None:
        metadata["reasoning_id"] = extracted["reasoning_id"]
    message_metadata = response_metadata.get("message_metadata")
    if message_metadata is not None:
        if not isinstance(message_metadata, Mapping):
            raise TypeError("checkpoint message_metadata 必须是对象")
        metadata.update(
            {str(key): value for key, value in message_metadata.items()}
        )
    metadata.update(
        {
            key: value
            for key, value in response_metadata.items()
            if key
            not in {
                "attachments",
                "content_blocks",
                "message_metadata",
                "message_role",
            }
        }
    )
    if structured_metadata is not None:
        if display_projection is None:
            raise RuntimeError("内部结构消息缺少展示投影")
        metadata = {
            "langchain_type": message.type,
            **display_projection.metadata,
        }
    return MessageDTO(
        message_id=message_id,
        session_id=session_id,
        thread_id=thread_id,
        role=role,
        content=content,
        attachments=_attachments_for_message(
            message,
            response_metadata,
            user_projection,
        ),
        metadata=metadata,
        created_at=created_at,
        updated_at=updated_at,
    )


def _metadata_datetime(metadata: Mapping[object, object], key: str) -> datetime:
    value = metadata.get(key)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
    else:
        raise RuntimeError(f"用户可见消息缺少持久化 {key}")
    if parsed.tzinfo is None:
        raise RuntimeError(f"用户可见消息的持久化 {key} 必须包含时区")
    return parsed


def message_role(message: BaseMessage) -> MessageRole:
    """模型消息类型决定 role，业务来源只保存在 response_metadata。"""
    if isinstance(message, HumanMessage):
        return MessageRole.user
    if isinstance(message, AIMessage):
        return MessageRole.assistant
    if isinstance(message, ToolMessage):
        return MessageRole.tool
    return MessageRole.system


def json_message_value(value: object) -> object:
    """将消息字段独立复制为 JSON 兼容值，保留原生内容块布局。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): json_message_value(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [json_message_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return json_message_value(model_dump(mode="json"))
    return str(value)


def attachments_from_message_metadata(metadata: Mapping[object, object]) -> list[AttachmentRef]:
    raw_attachments = metadata.get("attachments")
    if not isinstance(raw_attachments, Sequence) or isinstance(
        raw_attachments, (str, bytes, bytearray)
    ):
        return []

    attachments: list[AttachmentRef] = []
    for item in raw_attachments:
        if isinstance(item, AttachmentRef):
            attachments.append(item.model_copy(update={"data_url": None}))
            continue
        if isinstance(item, Mapping):
            attachments.append(AttachmentRef.model_validate({
                str(key): value
                for key, value in item.items()
                if str(key) != "data_url"
            }))
            continue
        raise TypeError(
            f"message.response_metadata.attachments 中出现不支持的元素类型: {type(item).__name__}"
        )
    return attachments


def _attachments_for_message(
    message: BaseMessage,
    response_metadata: Mapping[object, object],
    user_projection: UserContentProjection | None,
) -> list[AttachmentRef]:
    attachments = attachments_from_message_metadata(response_metadata)
    if attachments or not isinstance(message, HumanMessage):
        return attachments
    projection = user_projection or user_content_projection(
        message.content,
        response_metadata,
    )
    return [
        AttachmentRef.model_validate(
            {
                str(key): value
                for key, value in item.items()
                if str(key) != "data_url"
            }
        )
        for item in projection.attachments
    ]


def is_user_visible_message(message: BaseMessage) -> bool:
    metadata = message.response_metadata or {}
    message_metadata = metadata.get("message_metadata")
    if isinstance(message.content, str) and internal_prompt_metadata(metadata) is not None:
        return project_message_for_display(
            message.content,
            metadata,
        ).visible
    # 结构化提醒只有经验证的 display projection 可以公开；没有该投影的
    # canonical internal carrier 不能因使用 HumanMessage 就成为用户输入。
    if metadata.get("internal") is True or (
        isinstance(message_metadata, Mapping)
        and message_metadata.get("internal") is True
    ):
        return False
    if isinstance(message, ToolMessage):
        return False
    if isinstance(message, AIMessage):
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            return False
        extracted = extract_message_content(message)
        content = extracted["content"]
        return isinstance(content, str) and bool(content.strip())
    return True


def extract_message_content(message: BaseMessage) -> dict[str, object]:
    """从 BaseMessage 提取可读 content，并把结构化 reasoning 块单独保存。

    返回:
    - content: 用户可见正文，不包含 reasoning
    - content_blocks: LangChain 标准 content block 列表
    - reasoning_id: 首个 reasoning 块的 id（用于关联）
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return {
            "content": content,
            "content_blocks": [],
            "reasoning_id": None,
        }

    if not isinstance(content, list):
        return {
            "content": str(content),
            "content_blocks": [],
            "reasoning_id": None,
        }

    text_parts: list[str] = []
    content_blocks: list[dict[str, object]] = []
    reasoning_id: str | None = None
    raw_part_refs = (message.response_metadata or {}).get("content_part_refs")
    part_refs = raw_part_refs if isinstance(raw_part_refs, list) else []

    def part_ref(position: int) -> Mapping[object, object] | None:
        if position >= len(part_refs) or not isinstance(part_refs[position], Mapping):
            return None
        return part_refs[position]

    for part in content:
        content_position = len(content_blocks)
        if not isinstance(part, dict):
            text = str(part)
            text_parts.append(text)
            content_blocks.append({"type": "text", "text": text})
            continue
        part_type = part.get("type")
        ref = part_ref(content_position)
        if part_type in ("text", "output_text"):
            text = part.get("text", "")
            if isinstance(text, str):
                text_parts.append(text)
                text_block: dict[str, object] = {"type": "text", "text": text}
                if isinstance(part.get("id"), str):
                    text_block["id"] = part["id"]
                if isinstance(part.get("index"), int):
                    text_block["index"] = part["index"]
                if ref is not None:
                    if "id" not in text_block and isinstance(ref.get("id"), str):
                        text_block["id"] = ref["id"]
                    if "index" not in text_block and isinstance(ref.get("index"), int):
                        text_block["index"] = ref["index"]
                content_blocks.append(text_block)
        elif part_type in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "thinking",
        }:
            reasoning_text, _ = split_agent_content([part])
            reasoning_block: dict[str, object] = {
                "type": "reasoning",
                "reasoning": reasoning_text,
            }
            if reasoning_id is None:
                rid = part.get("id")
                if isinstance(rid, str):
                    reasoning_id = rid
            if isinstance(part.get("id"), str):
                reasoning_block["id"] = part["id"]
            if isinstance(part.get("index"), int):
                reasoning_block["index"] = part["index"]
            if ref is not None:
                if "id" not in reasoning_block and isinstance(ref.get("id"), str):
                    reasoning_block["id"] = ref["id"]
                if "index" not in reasoning_block and isinstance(ref.get("index"), int):
                    reasoning_block["index"] = ref["index"]
            content_blocks.append(reasoning_block)
        elif part_type == "refusal":
            refusal_text = part.get("refusal", "")
            text_parts.append(f"[拒绝]{refusal_text}")
            content_blocks.append({"type": "text", "text": f"[拒绝]{refusal_text}"})
        elif part_type == "image_url":
            image_url = part.get("image_url")
            if isinstance(image_url, Mapping):
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": json_message_value(image_url),
                    }
                )
            elif isinstance(image_url, str):
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    }
                )
        elif part_type == "image":
            content_blocks.append(
                {
                    str(key): json_message_value(value)
                    for key, value in part.items()
                }
            )
        elif part_type == "function_call":
            name = part.get("name", "unknown_tool")
            args = part.get("arguments", "")
            text_parts.append(f"[调用工具 {name}，参数：{args}]")
        else:
            # 其它未知块类型：尝试提取常见字段，避免直接丢弃
            fallback = part.get("text")
            if isinstance(fallback, str):
                text_parts.append(fallback)
                content_blocks.append({"type": "text", "text": fallback})

    return {
        "content": "".join(text_parts),
        "content_blocks": content_blocks,
        "reasoning_id": reasoning_id,
    }


def _display_content_blocks(value: object) -> list[dict[str, object]]:
    """只保留历史 UI 所需的轻量文本/推理块，媒体正文由附件接口读取。"""
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for block in value:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        block_metadata = block.get("metadata")
        if (
            isinstance(block_metadata, Mapping)
            and block_metadata.get("origin") == "generated"
            and block_metadata.get("kind") in {
                "attachment_manifest",
                "attachment_preview",
            }
        ):
            continue
        if block_type == "text" and isinstance(block.get("text"), str):
            normalized: dict[str, object] = {
                "type": "text",
                "text": block["text"],
            }
        elif block_type == "reasoning" and isinstance(
            block.get("reasoning"), str
        ):
            normalized = {
                "type": "reasoning",
                "reasoning": block["reasoning"],
            }
        else:
            continue
        if isinstance(block.get("id"), str):
            normalized["id"] = block["id"]
        if isinstance(block.get("index"), int):
            normalized["index"] = block["index"]
        result.append(normalized)
    return result
