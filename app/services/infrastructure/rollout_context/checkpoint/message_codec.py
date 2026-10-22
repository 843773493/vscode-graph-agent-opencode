"""LangChain 消息与 v2 canonical item 的唯一适配器。

存储层只依赖 ``MessageCodec`` 端口；本模块是 checkpoint/integration 边界，
负责把 LangChain 的临时消息 view 转成 domain item，或从 item 恢复临时消息。
它不写 JSONL/SQLite，也不拥有 canonical payload。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)

from app.agents.tool_result_text import tool_output_status
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.hashing import canonical_json_bytes, content_hash
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
    ItemSourceManifest,
    ProjectionPartRef,
)
from app.domain.itemized.identity.tool_call_identity import (
    matches_tool_call_id,
    provider_tool_call_id,
)
from app.domain.itemized.records import (
    CanonicalItemRecord,
    InternalDisplayPart,
    InternalDisplayProvenance,
    RuntimeNoticePayload,
    user_input_payload,
)
from app.prompting.factory import INTERNAL_PROMPT_SCHEMA_VERSION
from app.prompting.registry import (
    InternalDisplayPolicy,
    structured_prompt_registry,
)
from app.prompting.validation import internal_prompt_metadata, validate_internal_message
from app.services.infrastructure.rollout_context.checkpoint.codec.groups import (
    expand_tool_message,
    group_content,
    message_projection_id,
    restore_group_manifests,
    validate_group,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    CANONICAL_GROUP_FIELD,
    TOOL_BINDING_FIELD,
    TOOL_BINDING_KEYS,
    message_annotations,
    project_annotations,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.mapping.itemized.canonical_langchain import project_tool_result
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


def _internal_display_provenance(
    response_metadata: Mapping[str, object],
) -> InternalDisplayProvenance:
    raw = response_metadata.get("internal_display_provenance")
    nested = response_metadata.get("message_metadata")
    nested_value = (
        nested.get("internal_display_provenance")
        if isinstance(nested, Mapping)
        else None
    )
    if raw is not None and nested_value is not None and raw != nested_value:
        raise ValueError("internal display provenance 在顶层与 message_metadata 中冲突")
    value = raw if raw is not None else nested_value
    if value is None:
        return InternalDisplayProvenance()
    return InternalDisplayProvenance.from_dict(value)


def _runtime_notice_payload(
    prompt: object,
    response_metadata: Mapping[str, object],
) -> RuntimeNoticePayload | None:
    metadata = internal_prompt_metadata(response_metadata)
    if metadata is None:
        return None
    if not isinstance(prompt, str):
        raise TypeError("内部结构消息 content 必须是字符串")
    validate_internal_message(prompt, response_metadata)
    kind = metadata.get("structured_prompt_kind")
    if not isinstance(kind, str):
        raise TypeError("内部结构消息 structured_prompt_kind 必须是字符串")
    kind_spec = structured_prompt_registry.internal_message_kind(kind)
    raw_attachments = response_metadata.get("attachments", [])
    raw_blocks = response_metadata.get("attachment_blocks", [])
    if not isinstance(raw_attachments, list) or not isinstance(raw_blocks, list):
        raise TypeError("内部结构消息附件及生成块必须是 array")
    if kind_spec.display_policy != InternalDisplayPolicy.explicit and not raw_attachments:
        return None
    display_kind = kind_spec.display_kind
    display_text = metadata.get("display_content")
    explicit = kind_spec.display_policy == InternalDisplayPolicy.explicit
    if explicit and (display_kind is None or not isinstance(display_text, str)):
        raise ValueError(f"显式展示内部消息 kind={kind} 缺少 typed display")
    return RuntimeNoticePayload(
        prompt=prompt,
        display_part=InternalDisplayPart(
            kind=display_kind,
            text=display_text,
            provenance=_internal_display_provenance(response_metadata),
        ) if explicit else None,
        attachments=tuple({key: value for key, value in attachment.items() if value is not None}
                          for attachment in raw_attachments),
        attachment_blocks=tuple(raw_blocks),
    )


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
        resolved: str | None = None
        for field, value in (
            ("response_metadata.model_call_id", metadata.get("model_call_id")),
            (
                "message_metadata.model_call_id",
                self._message_metadata(message).get("model_call_id"),
            ),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field} 必须是非空字符串或 None: {value!r}")
            if resolved is not None and value != resolved:
                raise ValueError(
                    "checkpoint message model_call_id 身份不一致: "
                    f"message_id={getattr(message, 'id', None)!r}, "
                    f"model_call_id={resolved!r}, {field}={value!r}"
                )
            resolved = value
        if resolved is not None:
            return resolved
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
            identity = ItemProjectionIdentity(model_call_id=model_call_id)
            if any(
                isinstance(call.get("id") or call.get("tool_call_id"), str)
                and matches_tool_call_id(
                    identity, call.get("id") or call["tool_call_id"], tool_call_id,
                )
                for call in self.tool_calls(preceding)
            ):
                return model_call_id
        return None

    def tool_result_call_ids(self, messages: Sequence[object]) -> tuple[str, ...]:
        """从真实 ToolMessage 与其 emitting assistant 取得结果的 scoped identity。"""
        call_ids = []
        for index, message in enumerate(messages):
            if not isinstance(message, ToolMessage):
                continue
            model_call_id = self.tool_message_model_call_id(message, messages[:index])
            identity = ItemProjectionIdentity(model_call_id=model_call_id)
            raw_call_id = provider_tool_call_id(identity, message.tool_call_id)
            call_ids.append(
                f"{model_call_id}:tool-call:{raw_call_id}"
                if model_call_id is not None else raw_call_id
            )
        return tuple(dict.fromkeys(call_ids))

    def items_for_message(
        self,
        message: object,
        *,
        item_sequence: int,
        message_id: str,
        turn_id: str | None,
        execution_id: str | None = None,
        timestamp: str,
        model_call_id: str | None = None,
        canonical_items: Sequence[CanonicalItemRecord] = (),
    ) -> tuple[CanonicalItemRecord, ...]:
        """timestamp 是消息 canonical 业务 UTC 时刻；fork 保留源 item.created_at。

        canonical_items 只接受 locator 读出的完整已存消息组，仅复用 item_id/
        message_group_id；正文与 typed 元数据重新解析，调用方完整比较后引用原组。
        空序列走新身份，候选 item_sequence 不改变已存组的索引或写入坐标。
        """
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise ValueError("items_for_message.turn_id 必须是非空字符串或 None")
        if execution_id is not None and (
            not isinstance(execution_id, str) or not execution_id
        ):
            raise ValueError("items_for_message.execution_id 必须是非空字符串或 None")
        if turn_id is None and execution_id is None:
            raise ValueError(
                "Turn-less canonical message 必须带 admission 的 execution_id"
            )
        message_model_call_id = self.model_call_id(message)
        if model_call_id is None:
            model_call_id = message_model_call_id
        elif message_model_call_id is not None and model_call_id != message_model_call_id:
            raise ValueError(
                "items_for_message.model_call_id 与消息自身身份不一致: "
                f"message_id={message_id!r}, caller={model_call_id!r}, "
                f"message={message_model_call_id!r}"
            )
        message_value = self.to_dict(message)
        data = message_value.get("data")
        data_mapping = data if isinstance(data, Mapping) else {}
        if canonical_items and canonical_items[-1].semantic_kind == SemanticKind.COMPACTION_SUMMARY:
            stored = canonical_items[-1]
            if (len(canonical_items) != 1 or not isinstance(message, HumanMessage)
                or turn_id != stored.turn_id or message_id != message_projection_id(stored)
                or execution_id != stored.producer_ref.get("invocation_id")):
                raise ValueError("compaction summary 必须复用同一执行归属的唯一 canonical 来源")
            metadata = self._metadata(message)
            if metadata.get("compaction_summary") is not True or metadata.get("internal") is not True:
                raise ValueError("compaction summary 缺少真实 typed 消息标记")
            raw_relations = metadata.get("provenance_relations", [])
            if not isinstance(raw_relations, list) or tuple(ItemProvenanceRelation.from_dict(entry) for entry in raw_relations) != stored.provenance_relations:
                raise ValueError("compaction summary 的来源关系发生变化")
            payload = {**stored.payload, "summary": data_mapping.get("content")}
            return (replace(stored, item_sequence=item_sequence, payload=payload,
                            content_hash=content_hash(PayloadKind.SUMMARY, payload)),)
        role = self.message_role(message)
        internal = self.is_internal(message)
        content = data_mapping.get("content")
        response_metadata = self._metadata(message)
        item_status = CanonicalItemStatus.COMPLETED
        display_payload = (
            _runtime_notice_payload(content, response_metadata)
            if (
                isinstance(message, HumanMessage)
                and internal_prompt_metadata(response_metadata) is not None
            )
            else None
        )
        nested_metadata = response_metadata.get("message_metadata")
        nested_metadata = (
            nested_metadata if isinstance(nested_metadata, Mapping) else {}
        )
        phase = response_metadata.get("phase", nested_metadata.get("phase"))
        refs = response_metadata.get(
            "content_part_refs", nested_metadata.get("content_part_refs", [])
        )
        if not isinstance(refs, list):
            raise TypeError("checkpoint content_part_refs 必须是有序 array")
        identity = ItemProjectionIdentity(
            projection_message_id=message_id,
            model_call_id=model_call_id,
            phase=phase,
            completion_reason=response_metadata.get(
                "completion_reason", nested_metadata.get("completion_reason")
            ),
            content_part_refs=tuple(ProjectionPartRef.from_dict(ref) for ref in refs),
        )
        raw_relations = response_metadata.get("provenance_relations", [])
        if not isinstance(raw_relations, list):
            raise TypeError("checkpoint provenance_relations 必须是有序 typed array")
        relations = tuple(
            ItemProvenanceRelation.from_dict(entry) for entry in raw_relations
        )
        extensions = message_annotations(response_metadata)
        if role == "user" and not internal:
            content = user_input_payload(content, response_metadata.get("attachments"))
        internal_notice = (
            internal
            and isinstance(message, HumanMessage)
            and (turn_id is None or display_payload is not None)
        )
        if internal_notice:
            semantic_kind = SemanticKind.RUNTIME_NOTICE
            if display_payload is not None:
                if execution_id is None:
                    raise ValueError(
                        "显式 internal display notice 必须绑定 admission execution_id"
                    )
                payload_kind = PayloadKind.STRUCTURED_CONTENT
                payload = display_payload.to_dict()
            else:
                payload_kind = PayloadKind.TEXT
                payload = _stringify_content(content)
            item_turn_id: str | None = None
            turn_scope: str | None = TurnScope.PENDING_NEXT_TURN
        elif isinstance(message, HumanMessage):
            semantic_kind = SemanticKind.USER_INPUT
            if turn_id is None:
                raise ValueError("user_input root 必须带已接受的 turn_id")
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
            tool_call_id = data_mapping.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ValueError("ToolMessage 缺少真实 tool_call_id")
            tool_outcome = response_metadata.get(
                "tool_outcome", "success" if tool_output_status(message) == "success" else "failure",
            )
            confirmed = response_metadata.get("execution_confirmed", tool_outcome != "unknown")
            item_status = response_metadata.get("canonical_item_status", CanonicalItemStatus.COMPLETED)
            payload = {
                "tool_call_id": tool_call_id,
                "result_id": message_id,
                "name": str(data_mapping.get("name") or "tool"),
                "content": data_mapping.get("content"),
                "tool_outcome": tool_outcome,
                "execution_confirmed": confirmed,
            }
            item_turn_id = turn_id
            turn_scope = TurnScope.TURN_MEMBER if turn_id is not None else None
        elif self.tool_calls(message_value):
            semantic_kind = SemanticKind.TOOL_CALL
            payload_kind = PayloadKind.TOOL_CALL
            payload = {
                "tool_calls": [dict(call) for call in self.tool_calls(message_value)]
            }
            item_turn_id = turn_id
            turn_scope = TurnScope.TURN_MEMBER if turn_id is not None else None
        else:
            semantic_kind = SemanticKind.ASSISTANT_OUTPUT
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
            turn_scope = TurnScope.TURN_MEMBER if turn_id is not None else None
        invocation_id = execution_id if execution_id is not None else turn_id
        producer_kind = (
            "user"
            if semantic_kind == SemanticKind.USER_INPUT
            else "runtime"
            if internal
            else "provider"
        )
        primary = CanonicalItemRecord.create(
            item_sequence=item_sequence,
            item_id=canonical_items[-1].item_id if canonical_items else f"item-{message_id}",
            semantic_kind=semantic_kind,
            payload_kind=payload_kind,
            status=item_status,
            producer_ref={
                "producer_kind": producer_kind,
                "producer_id": message_id,
                **(
                    {"invocation_id": invocation_id}
                    if invocation_id is not None
                    else {}
                ),
            },
            payload=payload,
            created_at=timestamp,
            extensions=extensions,
            projection_identity=identity,
            source_manifest=ItemSourceManifest(
                response_metadata["context_revision"], "public", "public"
            )
            if response_metadata.get("context_revision") is not None
            else None,
            provenance_relations=relations,
            turn_id=item_turn_id,
            turn_scope=turn_scope,
            message_group_id=canonical_items[-1].message_group_id if canonical_items else f"message-{message_id}",
            wire_role=role,
        )
        group = expand_tool_message(primary, data_mapping.get("content", ""))
        if canonical_items and len(group) == len(canonical_items):
            # 身份只来自 locator 读出的规范组；正文和 typed 镜像仍须逐成员重验。
            group = tuple(
                replace(item, item_id=stored.item_id, message_group_id=stored.message_group_id)
                for item, stored in zip(group, canonical_items, strict=True)
            )
        return restore_group_manifests(group, response_metadata)

    def project_message(
        self,
        items: Sequence[CanonicalItemRecord],
    ) -> dict[str, object]:
        item = validate_group(items)
        message_id = message_projection_id(item)
        if item.semantic_kind == SemanticKind.USER_INPUT:
            message: BaseMessage = HumanMessage(
                content=_message_content_value(
                    item.payload.get("content", item.payload)
                    if isinstance(item.payload, Mapping)
                    and "attachments" in item.payload
                    else item.payload
                ),
                id=message_id,
            )
        elif item.semantic_kind == SemanticKind.COMPACTION_SUMMARY:
            message = HumanMessage(content=item.payload["summary"], id=message_id,
                                   response_metadata={"internal": True, "compaction_summary": True})
        elif item.semantic_kind == SemanticKind.RUNTIME_NOTICE:
            # runtime notice 仍沿用 checkpoint 的 HumanMessage wire projection，
            # 但通过 canonical semantic kind 恢复 internal 标记，避免它被
            # 历史读取误当成真实用户 Turn。
            if (
                item.payload_kind == PayloadKind.STRUCTURED_CONTENT
                and isinstance(item.payload, Mapping)
                and set(item.payload) & {"prompt", "display_part"}
            ):
                notice_payload = RuntimeNoticePayload.from_dict(item.payload)
                display = notice_payload.display_part
                response_metadata: dict[str, object] = {
                    "internal": True,
                    "structured_prompt_kind": display.kind if display is not None else project_annotations(item).get("structured_prompt_kind"),
                    "structured_prompt_schema_version": INTERNAL_PROMPT_SCHEMA_VERSION,
                }
                if display is not None:
                    response_metadata.update(
                        internal_display_kind=display.kind, display_content=display.text,
                        internal_display_provenance=display.provenance.to_dict(),
                    )
                if notice_payload.attachments:
                    response_metadata["attachment_blocks"] = [dict(block) for block in notice_payload.attachment_blocks]
                validate_internal_message(notice_payload.prompt, response_metadata)
                message = HumanMessage(
                    content=notice_payload.prompt,
                    id=message_id,
                    response_metadata=response_metadata,
                )
            else:
                message = HumanMessage(
                    content=_message_content_value(item.payload),
                    id=message_id,
                    response_metadata={"internal": True},
                )
        elif item.semantic_kind == SemanticKind.TOOL_RESULT:
            message = project_tool_result(item).model_copy(update={"id": message_id})
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
                        "id": provider_tool_call_id(
                            group_item.projection_identity,
                            str(call.get("id") or call.get("tool_call_id") or group_item.item_id),
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
                id=message_id,
                tool_calls=tool_calls,
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
        data = projected.get("data")
        if not isinstance(data, Mapping):
            raise TypeError(f"message projection data 必须是 object: {item.item_id}")
        response = data.get("response_metadata")
        merged = dict(response) if isinstance(response, Mapping) else {}
        merged.update(project_annotations(item))
        if item.semantic_kind == SemanticKind.RUNTIME_NOTICE:
            merged["internal"] = True
            merged["message_metadata"] = {
                **merged.get("message_metadata", {}),
                "internal": True,
            }
        if item.source_manifest is not None:
            merged["context_revision"] = item.source_manifest.source_revision
        if item.projection_identity is not None:
            identity = item.projection_identity
            if identity.completion_reason is not None:
                merged["completion_reason"] = identity.completion_reason
            if identity.phase is not None:
                merged["phase"] = identity.phase
            if identity.model_call_id is not None:
                merged["model_call_id"] = identity.model_call_id
            if identity.content_part_refs:
                merged["content_part_refs"] = [
                    ref.to_dict() for ref in identity.content_part_refs
                ]
        merged["provenance_relations"] = [
            relation.to_dict() for relation in item.provenance_relations
        ]
        manifests = []
        for member in items:
            manifest = {"item_id": member.item_id, **member.projection_manifest()}
            if (
                member.semantic_kind in TOOL_BINDING_KEYS
                and isinstance(member.payload, Mapping)
                and "tool_calls" not in member.payload
            ):
                # 保留调用/结果 carrier 的生命周期 ID，不复制 name/args/content 正文。
                manifest[TOOL_BINDING_FIELD] = {
                    key: member.payload[key] for key in TOOL_BINDING_KEYS[member.semantic_kind] if key in member.payload
                }
            manifests.append(manifest)
        merged[CANONICAL_GROUP_FIELD] = manifests
        if isinstance(item.payload, Mapping) and isinstance(
            item.payload.get("attachments"), list
        ):
            merged["attachments"] = [
                dict(attachment) for attachment in item.payload["attachments"]
            ]
        if merged:
            projected["data"] = {**data, "response_metadata": merged}
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
                item.semantic_kind == SemanticKind.RUNTIME_NOTICE
                and item.payload_kind == PayloadKind.STRUCTURED_CONTENT
                and isinstance(item.payload, Mapping)
                and set(item.payload) & {"prompt", "display_part"}
            ):
                return RuntimeNoticePayload.from_dict(item.payload).prompt
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
