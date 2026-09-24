"""Message DTO、附件与 Agent State 的无 I/O 映射。

这里不读取 checkpoint/storage，也不决定业务状态；``MessageService`` 只
通过该 mixin 使用纯转换结果。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.prompting.validation import internal_prompt_metadata, validate_internal_message
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import AttachmentRef, MessageDTO
from app.services.business.message_display import project_message_for_display
from app.services.mapping.agent_content_mapper import extract_reasoning_summary
from app.services.mapping.user_message_content_projection import user_content_projection


class MessageContentProjectionMixin:
    @staticmethod
    def _message_to_dto(
        session_id: str,
        thread_id: str,
        index: int,
        message: BaseMessage,
    ) -> MessageDTO:
        role = MessageContentProjectionMixin._persisted_role(message)
        extracted = MessageContentProjectionMixin._extract_content(message)
        content = extracted["content"]
        response_metadata = message.response_metadata or {}
        structured_metadata = internal_prompt_metadata(response_metadata)
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
        created_at = MessageContentProjectionMixin._metadata_datetime(response_metadata, "created_at")
        updated_at = MessageContentProjectionMixin._metadata_datetime(response_metadata, "updated_at")
        metadata: dict[str, object] = {
            "langchain_type": message.type,
            "tool_calls": getattr(message, "tool_calls", None) or [],
            "tool_call_id": getattr(message, "tool_call_id", None),
        }
        display_blocks = MessageContentProjectionMixin._display_content_blocks(
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
            attachments=MessageContentProjectionMixin._attachments_for_message(
                message,
                response_metadata,
            ),
            metadata=metadata,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
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

    @staticmethod
    def _detect_role(message: BaseMessage) -> MessageRole:
        if isinstance(message, HumanMessage):
            return MessageRole.user
        if isinstance(message, AIMessage):
            return MessageRole.assistant
        if isinstance(message, ToolMessage):
            return MessageRole.tool
        return MessageRole.system

    @staticmethod
    def _persisted_role(message: BaseMessage) -> MessageRole:
        """模型消息类型决定 role，业务来源只保存在 response_metadata。"""
        return MessageContentProjectionMixin._detect_role(message)

    @staticmethod
    def _json_safe(value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {
                str(key): MessageContentProjectionMixin._json_safe(item) for key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [MessageContentProjectionMixin._json_safe(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return MessageContentProjectionMixin._json_safe(model_dump(mode="json"))
        return str(value)

    @staticmethod
    def _attachments_from_metadata(metadata: Mapping[object, object]) -> list[AttachmentRef]:
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

    @staticmethod
    def _attachments_for_message(
        message: BaseMessage,
        response_metadata: Mapping[object, object],
    ) -> list[AttachmentRef]:
        attachments = MessageContentProjectionMixin._attachments_from_metadata(response_metadata)
        if attachments or not isinstance(message, HumanMessage):
            return attachments
        projection = user_content_projection(message.content, response_metadata)
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

    @staticmethod
    def _is_system_reminder_only_message(message: BaseMessage) -> bool:
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            return False
        metadata = message.response_metadata or {}
        if internal_prompt_metadata(metadata) is None:
            return False
        validate_internal_message(content, metadata)
        return True

    @staticmethod
    def _is_user_visible_message(message: BaseMessage) -> bool:
        metadata = message.response_metadata or {}
        message_metadata = metadata.get("message_metadata")
        if MessageContentProjectionMixin._is_system_reminder_only_message(message):
            content = message.content
            if not isinstance(content, str):
                raise TypeError("内部结构消息 content 必须是字符串")
            return project_message_for_display(
                content,
                message.response_metadata or {},
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
            extracted = MessageContentProjectionMixin._extract_content(message)
            content = extracted["content"]
            return isinstance(content, str) and bool(content.strip())
        return True

    @staticmethod
    def _message_to_agent_state_record(message: BaseMessage) -> dict[str, object]:
        extracted = MessageContentProjectionMixin._extract_content(message)
        content_blocks = extracted["content_blocks"]
        raw_content = getattr(message, "content", "")
        record: dict[str, object] = {
            "role": MessageContentProjectionMixin._persisted_role(message).value,
            "type": message.type,
            "content": MessageContentProjectionMixin._json_safe(
                raw_content
                if isinstance(message, HumanMessage)
                else content_blocks if content_blocks else raw_content
            ),
        }
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            record["tool_calls"] = MessageContentProjectionMixin._json_safe(tool_calls)

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
            record["response_metadata"] = MessageContentProjectionMixin._json_safe(response_metadata)

        usage_metadata = getattr(message, "usage_metadata", None)
        if usage_metadata:
            record["usage_metadata"] = MessageContentProjectionMixin._json_safe(usage_metadata)

        additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
        if additional_kwargs:
            record["additional_kwargs"] = MessageContentProjectionMixin._json_safe(additional_kwargs)

        return record

    @staticmethod
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
            record[key] = MessageContentProjectionMixin._json_safe(value)
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
            str(key): MessageContentProjectionMixin._json_safe(value)
            for key, value in message.items()
            if str(key) not in ignored_keys
        }

    @staticmethod
    def _agent_state_record_key(record: Mapping[str, object]) -> str:
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _dedupe_consecutive_agent_state_records(
        records: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        deduped: list[dict[str, object]] = []
        previous_key: str | None = None
        for record in records:
            current_key = MessageContentProjectionMixin._agent_state_record_key(record)
            if current_key == previous_key:
                continue
            deduped.append(record)
            previous_key = current_key
        return deduped

    @staticmethod
    def _extract_content(message: BaseMessage) -> dict[str, object]:
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
                reasoning_text = (
                    part.get("thinking") or part.get("text")
                    if part_type == "thinking"
                    else part.get("reasoning") or part.get("reasoning_content")
                )
                if not isinstance(reasoning_text, str):
                    raw_content = part.get("content")
                    if isinstance(raw_content, list):
                        reasoning_text = "".join(
                            item.get("text", "")
                            for item in raw_content
                            if isinstance(item, Mapping)
                            and item.get("type") in {"reasoning_text", "text"}
                            and isinstance(item.get("text"), str)
                        )
                    elif part_type == "reasoning_items":
                        reasoning_text = extract_reasoning_summary(
                            part.get("reasoning_items")
                        )
                    else:
                        reasoning_text = ""
                if not reasoning_text:
                    reasoning_text = extract_reasoning_summary(part.get("summary"))
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
                            "image_url": MessageContentProjectionMixin._json_safe(image_url),
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
                        str(key): MessageContentProjectionMixin._json_safe(value)
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

    @staticmethod
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
