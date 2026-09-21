"""LangChain 消息与 v2 canonical item 的唯一适配器。

存储层只依赖 ``MessageCodec`` 端口；本模块是 checkpoint/integration 边界，
负责把 LangChain 的临时消息 view 转成 domain item，或从 item 恢复临时消息。
它不写 JSONL/SQLite，也不拥有 canonical payload。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.codec.groups import (
    expand_tool_message,
    group_content,
    validate_group,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    restore_metadata,
    semantic_metadata,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.mapping.itemized.provider_history import (
    reasoning_projection_rows,
)
from app.services.mapping.itemized.provider_history import (
    visible_text as provider_visible_text,
)


def _message_content_value(value: object) -> object:
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (str, list)):
        return value
    return str(value)


def _stringify_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        block_type = value.get("type")
        if block_type in {"text", "output_text", "input_text"} and isinstance(
            value.get("text"), str
        ):
            return str(value["text"])
        return ""
    if isinstance(value, list):
        return "".join(
            part for part in (_stringify_content(item) for item in value) if part
        )
    return ""


def _serialized_tool_calls(value: Mapping[str, object]) -> list[Mapping[str, object]]:
    data = value.get("data")
    calls = data.get("tool_calls") if isinstance(data, Mapping) else None
    return (
        [call for call in calls if isinstance(call, Mapping)]
        if isinstance(calls, list)
        else []
    )


class LangChainMessageCodec:
    """LangChain 专用 codec；其输出永远是临时 projection。"""

    @staticmethod
    def _metadata(message: object) -> Mapping[str, object]:
        value = getattr(message, "response_metadata", {})
        return value if isinstance(value, Mapping) else {}

    @classmethod
    def _message_metadata(cls, message: object) -> Mapping[str, object]:
        value = cls._metadata(message).get("message_metadata")
        return value if isinstance(value, Mapping) else {}

    def message_role(self, message: object) -> str:
        if isinstance(message, HumanMessage):
            return "user"
        if isinstance(message, ToolMessage):
            return "tool"
        if isinstance(message, AIMessage):
            return "assistant"
        raise TypeError(f"rollout 不支持的消息类型: {type(message).__name__}")

    def to_dict(self, message: object) -> dict[str, object]:
        if not isinstance(message, BaseMessage):
            raise TypeError("消息 codec 只接受 LangChain BaseMessage")
        return message_to_dict(message)

    def is_internal(self, message: object) -> bool:
        return bool(
            self._metadata(message).get("internal") is True
            or self._message_metadata(message).get("internal") is True
        )

    def message_id(self, message: object, index: int) -> str:
        value = getattr(message, "id", None)
        if isinstance(value, str) and value:
            return value
        raw = self._metadata(message).get("message_id")
        if isinstance(raw, str) and raw:
            return raw
        digest = hashlib.sha256(
            canonical_json_bytes({"index": index, "message": self.to_dict(message)})
        ).hexdigest()[:32]
        return f"generated-{digest}"

    def turn_id(self, message: object, current: str | None, message_id: str) -> str:
        metadata = self._message_metadata(message)
        for value in (
            metadata.get("turn_id"),
            metadata.get("job_id"),
            self._metadata(message).get("turn_id"),
        ):
            if isinstance(value, str) and value:
                return value
        if self.is_internal(message):
            return f"internal-{hashlib.sha256(message_id.encode()).hexdigest()[:24]}"
        if isinstance(message, HumanMessage):
            return f"turn-{hashlib.sha256(message_id.encode()).hexdigest()[:24]}"
        return (
            current
            or f"internal-{hashlib.sha256(message_id.encode()).hexdigest()[:24]}"
        )

    def tool_calls(
        self, message: object | Mapping[str, object]
    ) -> list[Mapping[str, object]]:
        value = self.to_dict(message) if not isinstance(message, Mapping) else message
        return _serialized_tool_calls(value)

    def model_call_id(self, message: object) -> str | None:
        """从消息自身的持久 identity 读取所属模型调用。"""
        metadata = self._metadata(message)
        for value in (
            metadata.get("model_call_id"),
            self._message_metadata(message).get("model_call_id"),
        ):
            if isinstance(value, str) and value:
                return value
        message_id = getattr(message, "id", None)
        if isinstance(message_id, str) and message_id.startswith("lc_run--"):
            return message_id.removeprefix("lc_run--") or None
        return None

    def tool_message_model_call_id(
        self, message: object, preceding_messages: Sequence[object]
    ) -> str | None:
        """按消息顺序把 ToolMessage 绑定到它前面的 assistant carrier。"""
        direct = self.model_call_id(message)
        if direct is not None:
            return direct
        tool_call_id = getattr(message, "tool_call_id", None)
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return None
        for preceding in reversed(preceding_messages):
            model_call_id = self.model_call_id(preceding)
            if model_call_id is None:
                continue
            if any(
                call.get("id") == tool_call_id
                or call.get("tool_call_id") == tool_call_id
                for call in self.tool_calls(preceding)
            ):
                return model_call_id
        return None

    def items_for_message(
        self,
        message: object,
        *,
        item_sequence: int,
        message_id: str,
        turn_id: str,
        timestamp: str,
        model_call_id: str | None = None,
    ) -> tuple[CanonicalItemRecord, ...]:
        message_value = self.to_dict(message)
        data = message_value.get("data")
        data_mapping = data if isinstance(data, Mapping) else {}
        role = self.message_role(message)
        internal = self.is_internal(message)
        item_metadata: dict[str, object] = {
            "projection_message_id": message_id,
            "wire_role": role,
            "execution_confirmed": True,
        }
        if isinstance(model_call_id, str) and model_call_id:
            item_metadata["model_call_id"] = model_call_id
        response_metadata = self._metadata(message)
        item_metadata.update(semantic_metadata(response_metadata))
        for key in (
            "context_fork_source_session_id",
            "fork_source_session_id",
            "fork_source_item_id",
            "fork_source_turn_id",
        ):
            value = response_metadata.get(key)
            if isinstance(value, str) and value:
                item_metadata[key] = value
        raw_attachments = response_metadata.get("attachments")
        if isinstance(raw_attachments, list):
            attachments: list[dict[str, object]] = []
            for attachment in raw_attachments:
                if not isinstance(attachment, Mapping):
                    continue
                file_id = attachment.get("file_id")
                if not isinstance(file_id, str) or not file_id:
                    continue
                safe_attachment: dict[str, object] = {"file_id": file_id}
                for key in ("name", "content_type", "path", "preview_status"):
                    value = attachment.get(key)
                    if isinstance(value, str) and value:
                        safe_attachment[key] = value
                attachments.append(safe_attachment)
            if attachments:
                item_metadata["attachments"] = attachments
        message_turn_ids = tuple(
            value
            for name in ("turn_id", "job_id")
            if isinstance(
                value := self._message_metadata(message).get(name), str
            )
            and value
        )
        has_execution_turn = any(
            not value.startswith("internal-") for value in message_turn_ids
        )
        if internal and not (
            isinstance(message, HumanMessage) and has_execution_turn
        ):
            semantic_kind = SemanticKind.RUNTIME_NOTICE
            payload_kind = PayloadKind.TEXT
            payload = _stringify_content(data_mapping.get("content"))
            item_turn_id: str | None = None
            turn_scope: str | None = TurnScope.PENDING_NEXT_TURN
        elif isinstance(message, HumanMessage):
            semantic_kind = SemanticKind.USER_INPUT
            content = data_mapping.get("content")
            payload_kind = (
                PayloadKind.TEXT
                if isinstance(content, str)
                else PayloadKind.STRUCTURED_CONTENT
            )
            payload = content
            item_turn_id = turn_id
            turn_scope = TurnScope.TURN_ROOT
        elif isinstance(message, ToolMessage):
            semantic_kind = SemanticKind.TOOL_RESULT
            payload_kind = PayloadKind.TOOL_RESULT
            tool_call_id = str(data_mapping.get("tool_call_id") or "unknown-call")
            tool_status = data_mapping.get("status")
            tool_outcome = {
                "success": "success",
                "error": "failure",
            }.get(tool_status, "unknown")
            payload = {
                "tool_call_id": tool_call_id,
                "result_id": message_id,
                "name": str(data_mapping.get("name") or "tool"),
                "content": data_mapping.get("content"),
                "tool_outcome": tool_outcome,
            }
            item_metadata["tool_call_id"] = tool_call_id
            item_turn_id = turn_id
            turn_scope = TurnScope.TURN_MEMBER
        elif self.tool_calls(message_value):
            semantic_kind = SemanticKind.TOOL_CALL
            payload_kind = PayloadKind.TOOL_CALL
            payload = {
                "tool_calls": [dict(call) for call in self.tool_calls(message_value)]
            }
            item_turn_id = turn_id
            turn_scope = TurnScope.TURN_MEMBER
        else:
            semantic_kind = SemanticKind.ASSISTANT_OUTPUT
            content = data_mapping.get("content")
            invalid_tool_calls = data_mapping.get("invalid_tool_calls")
            if isinstance(invalid_tool_calls, list) and invalid_tool_calls:
                # LangChain 将无法解析的工具参数放在 invalid_tool_calls 中。
                # 这不是 provider wire 的可选诊断字段，而是用户可见消息的
                # 可恢复语义；必须随 canonical item 一起保存，不能只保留文本。
                payload_kind = PayloadKind.STRUCTURED_CONTENT
                payload = {
                    "content": content,
                    "invalid_tool_calls": [
                        dict(call)
                        for call in invalid_tool_calls
                        if isinstance(call, Mapping)
                    ],
                }
            else:
                payload_kind = (
                    PayloadKind.TEXT
                    if isinstance(content, str)
                    else PayloadKind.STRUCTURED_CONTENT
                )
                payload = content
            item_turn_id = turn_id
            turn_scope = TurnScope.TURN_MEMBER
        producer_kind = (
            "user"
            if semantic_kind == SemanticKind.USER_INPUT
            else "runtime"
            if internal
            else "provider"
        )
        primary = CanonicalItemRecord.create(
            item_sequence=item_sequence,
            item_id=f"item-{message_id}",
            semantic_kind=semantic_kind,
            payload_kind=payload_kind,
            status=CanonicalItemStatus.COMPLETED,
            producer_ref={
                "producer_kind": producer_kind,
                "producer_id": message_id,
                "invocation_id": turn_id,
            },
            payload=payload,
            created_at=timestamp,
            metadata=item_metadata,
            turn_id=item_turn_id,
            turn_scope=turn_scope,
            message_group_id=f"message-{message_id}",
            wire_role=role,
        )
        return expand_tool_message(primary, data_mapping.get("content", ""))

    def project_message(
        self, items: Sequence[CanonicalItemRecord],
    ) -> dict[str, object]:
        item = validate_group(items)
        message_id = item.metadata.get("projection_message_id")
        if not isinstance(message_id, str) or not message_id:
            message_id = item.item_id
        if item.semantic_kind == SemanticKind.USER_INPUT:
            message: BaseMessage = HumanMessage(
                content=_message_content_value(item.payload), id=message_id
            )
        elif item.semantic_kind == SemanticKind.RUNTIME_NOTICE:
            # runtime notice 仍沿用 checkpoint 的 HumanMessage wire projection，
            # 但通过 canonical semantic kind 恢复 internal 标记，避免它被
            # 历史读取误当成真实用户 Turn。
            message = HumanMessage(
                content=_message_content_value(item.payload),
                id=message_id,
                response_metadata={"internal": True},
            )
        elif item.semantic_kind == SemanticKind.TOOL_RESULT:
            payload = item.payload if isinstance(item.payload, Mapping) else {}
            tool_status = (
                "error"
                if payload.get("tool_outcome") in {"failure", "cancelled"}
                else "success"
            )
            message = ToolMessage(
                content=_message_content_value(payload.get("content", item.payload)),
                id=message_id,
                tool_call_id=str(payload.get("tool_call_id") or "unknown-call"),
                name=str(payload.get("name") or "tool"),
                status=tool_status,
            )
        elif item.semantic_kind == SemanticKind.TOOL_CALL:
            tool_calls: list[dict[str, object]] = []
            for group_item in items:
                if group_item.semantic_kind != SemanticKind.TOOL_CALL:
                    continue
                payload = (
                    group_item.payload
                    if isinstance(group_item.payload, Mapping)
                    else {}
                )
                raw_calls = payload.get("tool_calls")
                calls = raw_calls if isinstance(raw_calls, list) else [payload]
                tool_calls.extend(
                    {
                        "id": str(
                            call.get("id")
                            or call.get("tool_call_id")
                            or group_item.item_id
                        ),
                        "name": str(call.get("name") or "tool"),
                        "args": dict(call.get("args") or {})
                        if isinstance(call.get("args"), Mapping)
                        else {"raw": call.get("args", "")},
                        "type": "tool_call",
                    }
                    for call in calls
                    if isinstance(call, Mapping)
                )
            message = AIMessage(
                content=group_content(items) if len(items) > 1 else "",
                id=message_id, tool_calls=tool_calls,
            )
        else:
            if (
                isinstance(item.payload, Mapping)
                and "content" in item.payload
                and isinstance(item.payload.get("invalid_tool_calls"), list)
            ):
                invalid_tool_calls = [
                    dict(call)
                    for call in item.payload["invalid_tool_calls"]
                    if isinstance(call, Mapping)
                ]
                message = AIMessage(
                    content=_message_content_value(item.payload["content"]),
                    id=message_id,
                    invalid_tool_calls=invalid_tool_calls,
                )
            else:
                message = AIMessage(
                    content=_message_content_value(item.payload), id=message_id
                )
        projected = self.to_dict(message)
        if item.metadata:
            data = projected.get("data")
            if not isinstance(data, Mapping):
                raise TypeError(f"message projection data 必须是 object: {item.item_id}")
            response_metadata = data.get("response_metadata")
            merged_metadata = (
                dict(response_metadata)
                if isinstance(response_metadata, Mapping)
                else {}
            )
            merged_metadata.update(restore_metadata(item.metadata))
            for key in (
                "context_fork_source_session_id",
                "fork_source_session_id",
                "fork_source_item_id",
                "fork_source_turn_id",
            ):
                value = item.metadata.get(key)
                if isinstance(value, str) and value:
                    merged_metadata[key] = value
            attachments = item.metadata.get("attachments")
            if isinstance(attachments, list):
                merged_metadata["attachments"] = [
                    dict(attachment)
                    for attachment in attachments
                    if isinstance(attachment, Mapping)
                ]
            if merged_metadata:
                projected_data = dict(data)
                projected_data["response_metadata"] = merged_metadata
                projected["data"] = projected_data
        return projected

    def visible_text(self, message: Mapping[str, object]) -> str:
        data = message.get("data")
        content = data.get("content") if isinstance(data, Mapping) else None
        return (provider_visible_text(content) or _stringify_content(content))[
            : 64 * 1024
        ]

    def reasoning_rows(self, message: Mapping[str, object]) -> list[dict[str, object]]:
        data = message.get("data")
        content = data.get("content") if isinstance(data, Mapping) else None
        return [
            row
            for row in reasoning_projection_rows(content)
            if isinstance(row.get("kind"), str)
        ]

    def projection_content(self, item: CanonicalItemRecord) -> str:
        payload = item.payload
        if item.semantic_kind == SemanticKind.TOOL_CALL:
            raw_calls = (
                payload.get("tool_calls") if isinstance(payload, Mapping) else None
            )
            calls = raw_calls if isinstance(raw_calls, list) else [payload]
            return ", ".join(
                str(call.get("name"))
                for call in calls
                if isinstance(call, Mapping)
                and isinstance(call.get("name"), str)
                and call.get("name")
            )
        if item.semantic_kind == SemanticKind.TOOL_RESULT:
            value = payload.get("content") if isinstance(payload, Mapping) else None
            return provider_visible_text(value) or _stringify_content(value)
        if item.semantic_kind == SemanticKind.COMPACTION_SUMMARY:
            value = payload.get("summary") if isinstance(payload, Mapping) else None
            return provider_visible_text(value) or _stringify_content(value)
        if item.semantic_kind in {
            SemanticKind.USER_INPUT,
            SemanticKind.ASSISTANT_OUTPUT,
            SemanticKind.REASONING,
            SemanticKind.RUNTIME_NOTICE,
        }:
            if (
                item.semantic_kind == SemanticKind.ASSISTANT_OUTPUT
                and isinstance(payload, Mapping)
                and "content" in payload
            ):
                payload = payload["content"]
            return provider_visible_text(payload) or _stringify_content(payload)
        return ""

    def from_dict(self, value: object) -> object:
        if not isinstance(value, dict):
            raise TypeError("rollout message 必须是对象")
        return messages_from_dict([value])[0]


__all__ = ["LangChainMessageCodec", "MessageCodec"]
