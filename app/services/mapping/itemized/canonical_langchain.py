"""canonical item 到 LangChain 消息的无 I/O 叶映射。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
)

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.records import CanonicalItemRecord, RuntimeNoticePayload
from app.prompting.validation import internal_prompt_metadata, validate_internal_message
from app.services.mapping.itemized.carrier_dedup import (
    projection_message_group_id as _projection_message_group_id,
)
from app.services.mapping.itemized.carrier_dedup import (
    superseded_item_ids as _superseded_item_ids,
)
from app.services.mapping.itemized.provider_request import (
    project_runtime_notice_content,
)

_TOOL_PAYLOAD_RETRY_MAX_CHARS = 4096
_TOOL_PAYLOAD_RETRY_TOTAL_CHARS = 8192
_MEDIA_BLOCK_MARKERS = {
    "document": "[document]", "file": "[document]", "image": "[image]",
    "image_url": "[image]", "input_file": "[document]", "input_image": "[image]",
}

REASONING_CARRIER_TYPES = frozenset({
    "reasoning", "reasoning_content", "reasoning_items", "thinking", "redacted_thinking",
})


def _strip_media_content(content: object) -> tuple[object, bool]:
    if not isinstance(content, list):
        return content, False
    stripped: list[object] = []
    changed = False
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("type"), str) and block["type"] in _MEDIA_BLOCK_MARKERS:
            stripped.append({"type": "text", "text": _MEDIA_BLOCK_MARKERS[block["type"]]})
            changed = True
        elif isinstance(block, dict) and block.get("type") == "tool_result" and "content" in block:
            nested, nested_changed = _strip_media_content(block["content"])
            stripped.append({**block, "content": nested} if nested_changed else block)
            changed |= nested_changed
        else:
            stripped.append(block)
    return stripped, changed


def strip_media_from_summary_messages(messages: list[AnyMessage]) -> tuple[list[AnyMessage], bool]:
    """只变换摘要重试副本，canonical 媒体正文保持不变。"""
    result = []
    changed = False
    for message in messages:
        content, item_changed = _strip_media_content(message.content)
        result.append(message.model_copy(update={"content": content}) if item_changed else message)
        changed |= item_changed
    return result, changed


def _content_character_count(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return sum(_content_character_count(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_content_character_count(item) for item in value)
    return len(str(value))


def compact_large_tool_payloads_for_summary(messages: list[AnyMessage]) -> tuple[list[AnyMessage], bool]:
    """按单项和总载荷预算省略重试副本，工具身份及配对保持不变。"""
    payloads: list[tuple[str, int, int, int]] = []
    for message_index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            payloads.extend(("args", message_index, index, _content_character_count(call["args"]))
                            for index, call in enumerate(message.tool_calls))
        elif isinstance(message, ToolMessage):
            payloads.append(("result", message_index, -1, _content_character_count(message.content)))
    marked = {(kind, index, call) for kind, index, call, size in payloads if size > _TOOL_PAYLOAD_RETRY_MAX_CHARS}
    remaining = sum(size for kind, index, call, size in payloads if (kind, index, call) not in marked)
    for kind, index, call, size in payloads:
        if remaining <= _TOOL_PAYLOAD_RETRY_TOTAL_CHARS:
            break
        if (kind, index, call) not in marked:
            marked.add((kind, index, call))
            remaining -= size
    result = []
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage) and ("result", index, -1) in marked:
            result.append(message.model_copy(update={"content": "[large tool result omitted for compaction retry]"}))
        elif isinstance(message, AIMessage) and message.tool_calls:
            calls = [{**call, "args": {"_omitted": "large tool arguments"}}
                     if ("args", index, call_index) in marked else call
                     for call_index, call in enumerate(message.tool_calls)]
            result.append(message.model_copy(update={"tool_calls": calls}))
        else:
            result.append(message)
    return result, bool(marked)


def summary_source_groups(items: Sequence[CanonicalItemRecord]) -> dict[str, tuple[CanonicalItemRecord, ...]]:
    """同一物理消息的伴随 item 共用最后一个 anchor 的消息身份。"""
    groups: dict[str, list[CanonicalItemRecord]] = {}
    for item in items:
        groups.setdefault(item.message_group_id or item.item_id, []).append(item)
    return {_message_id(group[-1]): tuple(group) for group in groups.values()}


def project_summary_source(group: Sequence[CanonicalItemRecord]) -> BaseMessage:
    """恢复真实 checkpoint 消息正文，保留工具组原有 str/list 形状。"""
    originals = project_canonical_items(group, include_runtime_notices=True, include_summaries=True, preserve_order=True)
    if len(originals) != 1:
        raise ValueError("summary-source-mismatch: canonical 来源不是完整的唯一消息")
    expected = originals[0]
    anchor = group[-1]
    if anchor.semantic_kind == SemanticKind.RUNTIME_NOTICE and isinstance(anchor.payload, Mapping) and "prompt" in anchor.payload:
        # checkpoint 保留原始内部 XML；附件块只在 Provider 投影时组合。
        expected = expected.model_copy(update={"content": RuntimeNoticePayload.from_dict(anchor.payload).prompt})
    elif anchor.projection_group is not None:
        content: list[object] = []
        for item in group:
            if item.semantic_kind == SemanticKind.TOOL_CALL:
                continue
            if item.semantic_kind == SemanticKind.REASONING:
                carrier = project_reasoning_carrier(item)
                if carrier is None:
                    raise ValueError("summary-source-mismatch: reasoning carrier 不可投影")
                content.append(carrier)
            elif isinstance(item.payload, str):
                content.append(item.payload)
            else:
                content.extend(item.payload)
        expected = expected.model_copy(update={
            "id": _message_id(anchor),
            "content": content if anchor.projection_group.content_form == "list" else content[0] if content else "",
        })
    elif isinstance(expected, AIMessage) and isinstance(anchor.payload, Mapping) and "invalid_tool_calls" in anchor.payload:
        expected = expected.model_copy(update={"content": anchor.payload["content"], "invalid_tool_calls": anchor.payload["invalid_tool_calls"]})
    return expected


def summary_retry_variants(messages: Sequence[BaseMessage]) -> dict[str, tuple[BaseMessage, ...]]:
    """重试与冷校验使用相同中段及同一载荷预算，后缀裁剪不改变省略资格。"""
    stripped, _ = strip_media_from_summary_messages(list(messages))
    compacted, _ = compact_large_tool_payloads_for_summary(stripped)
    return {message.id: (stripped[index], compacted[index]) for index, message in enumerate(messages)}


def validate_summary_source(
    message: BaseMessage, group: Sequence[CanonicalItemRecord], *,
    retry_variants: Sequence[BaseMessage] = (),
) -> None:
    """准备与冷读取共同验证摘要副本，不能只相信冻结的来源 ID。"""
    if not group:
        metadata = internal_prompt_metadata(message.response_metadata)
        kind = metadata.get("structured_prompt_kind") if metadata is not None else None
        expected_kind = {"summarization_request": "compaction_summary_instruction", "summarization_retry": "compaction_retry_marker"}.get(message.response_metadata.get("lc_source"))
        if not isinstance(message, HumanMessage) or expected_kind is None or kind != expected_kind:
            raise ValueError(f"summary-source-mismatch: 候选消息没有 canonical 来源: {message.id!r}")
        validate_internal_message(message.content, message.response_metadata)
        return
    expected = project_summary_source(group)
    if expected.type != message.type or expected.id != message.id:
        raise ValueError("summary-source-mismatch: 候选角色或 canonical 消息身份不一致")
    # Chat 会在 typed calls 为空时读取 raw calls，并始终读取 function_call。
    # 这两类旁路不能通过同正文/同 ID 冒充 canonical 来源。
    sidecar_keys = ("function_call",) if isinstance(message, AIMessage) and message.tool_calls else ("tool_calls", "function_call")
    for key in sidecar_keys:
        if canonical_json_bytes(message.additional_kwargs.get(key)) != canonical_json_bytes(expected.additional_kwargs.get(key)):
            raise ValueError(f"summary-source-mismatch: 候选包含非 canonical wire sidecar: {key}")
    actual_data = message_to_dict(message)["data"]
    for candidate in (expected, *retry_variants):
        expected_data = message_to_dict(candidate)["data"]
        if all(canonical_json_bytes(actual_data.get(key)) == canonical_json_bytes(expected_data.get(key))
               for key in ("content", "tool_calls", "invalid_tool_calls", "tool_call_id", "name")):
            return
    raise ValueError(f"summary-source-mismatch: 候选正文不属于 canonical 投影或重试变体: {message.id}")


def _message_id(item: CanonicalItemRecord) -> str:
    value = (item.projection_identity.projection_message_id if item.projection_identity is not None else None)
    return value if isinstance(value, str) and value else item.item_id


def _model_call_metadata(item: CanonicalItemRecord) -> dict[str, object]:
    """按 codec 合同镜像已提交的 typed model-call 归属。"""
    identity = item.projection_identity
    if identity is None or identity.model_call_id is None:
        return {}
    return {"model_call_id": identity.model_call_id}


def _tool_calls(item: CanonicalItemRecord) -> list[dict[str, object]]:
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    raw_calls = payload.get("tool_calls")
    calls = raw_calls if isinstance(raw_calls, list) else [payload]
    result: list[dict[str, object]] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        result.append(
            {
                "id": provider_tool_call_id(
                    item.projection_identity,
                    str(call.get("id") or call.get("tool_call_id") or item.item_id),
                ),
                "name": str(call.get("name") or "tool"),
                "args": dict(call.get("args") or {})
                if isinstance(call.get("args"), Mapping)
                else {"raw": call.get("args", "")},
                "type": "tool_call",
            }
        )
    return result


def project_reasoning_carrier(item: CanonicalItemRecord) -> dict[str, object] | None:
    """恢复既有 canonical reasoning carrier；未知 envelope 保留 unsupported。"""
    if item.payload_kind == "opaque":
        return None
    if item.payload_kind == "extension":
        payload = item.payload
        if (
            payload.get("extension_schema") != "boxteam.checkpoint.reasoning-carrier"
            or payload.get("extension_version") != "1"
            or payload.get("encoding") != "json"
            or payload.get("schema_version") != "1"
        ):
            return None
        value = payload.get("value")
        if (
            not isinstance(value, Mapping)
            or value.get("type") not in REASONING_CARRIER_TYPES
            or value.get("type") != payload.get("wire_type")
        ):
            raise ValueError("canonical reasoning carrier 的 type/wire_type 不一致")
        return dict(value)
    identity = item.projection_identity
    if identity is not None and identity.reasoning_carrier is not None:
        carrier = identity.reasoning_carrier.to_dict()
        key = carrier.pop("text_key")
        return {**carrier, key: item.payload}
    return {
        "type": "reasoning",
        "text": item.payload.get("content", "")
        if isinstance(item.payload, Mapping)
        else item.payload,
        "item_id": item.item_id,
    }


def _message_content(value: object) -> object:
    """把 canonical payload 转成 LangChain 接受的 content 形态。

    LangChain 的消息模型接受字符串或 block 数组，不接受裸 JSON object。
    canonical item 仍保留原始结构；这里只做临时 projection，不能反写
    canonical 或 SQLite 正文。
    """
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (str, list)):
        return value
    return str(value)


def project_tool_result(item: CanonicalItemRecord) -> ToolMessage:
    """投影真实工具知识；传输 error 同时承载失败与未知，核心事实保持分离。"""
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    evidence = item.tool_result_evidence
    confirmed = evidence.execution_confirmed if evidence is not None else payload["execution_confirmed"]
    outcome = payload.get("tool_outcome", "success" if confirmed else "unknown")
    return ToolMessage(
        content=_message_content(payload.get("content", item.payload)),
        id=_message_id(item),
        tool_call_id=provider_tool_call_id(
            item.projection_identity,
            evidence.tool_call_id if evidence is not None else payload["tool_call_id"],
        ),
        name=str(payload.get("name") or "tool"),
        status="error" if outcome in {"failure", "cancelled", "unknown"} else "success",
        response_metadata={
            **_model_call_metadata(item),
            "canonical_item_status": item.status,
            "tool_outcome": outcome,
            "execution_confirmed": confirmed,
        },
    )


def project_canonical_items(
    items: Sequence[CanonicalItemRecord],
    *,
    include_runtime_notices: bool = False,
    include_summaries: bool = False,
    capability_losses: list[str] | None = None,
    preserve_order: bool = False,
) -> list[BaseMessage]:
    """按 canonical item 顺序生成 LangChain 临时 message view。"""
    messages: list[BaseMessage] = []
    message_groups: dict[str, int] = {}
    ordered = (
        items if preserve_order else sorted(items, key=lambda item: item.item_sequence)
    )
    superseded_ids = _superseded_item_ids(ordered)
    for item in ordered:
        if item.item_id in superseded_ids:
            continue
        if item.semantic_kind == SemanticKind.USER_INPUT:
            messages.append(
                HumanMessage(
                    content=_message_content(item.payload["content"] if isinstance(item.payload, Mapping) and "attachments" in item.payload else item.payload), id=_message_id(item)
                )
            )
            continue
        if item.semantic_kind == SemanticKind.TOOL_RESULT:
            messages.append(project_tool_result(item))
            continue
        if item.semantic_kind == SemanticKind.RUNTIME_NOTICE:
            if not include_runtime_notices:
                continue
            if item.wire_role != "user":
                raise ValueError(
                    "source-mismatch: runtime_notice 必须投影为 user: "
                    f"{item.item_id}"
                )
            content = project_runtime_notice_content(item.payload)
            if not content:
                continue
            messages.append(
                HumanMessage(
                    content=content,
                    id=_message_id(item),
                    response_metadata={
                        "internal": True,
                        "message_metadata": {
                            "turn_scope": item.turn_scope,
                            "item_id": item.item_id,
                        },
                    },
                )
            )
            continue
        if item.semantic_kind == SemanticKind.COMPACTION_SUMMARY:
            if not include_summaries:
                continue
            messages.append(
                HumanMessage(
                    content=item.payload["summary"],
                    id=_message_id(item),
                    response_metadata={"internal": True, "compaction_summary": True},
                )
            )
            continue
        if item.semantic_kind == SemanticKind.TOOL_CALL:
            group = _projection_message_group_id(item)
            calls = _tool_calls(item)
            current_index = message_groups.get(group)
            if (
                current_index == len(messages) - 1
                and current_index is not None
                and isinstance(messages[current_index], AIMessage)
            ):
                current = messages[current_index]
                messages[current_index] = current.model_copy(
                    update={"tool_calls": [*current.tool_calls, *calls]}
                )
            else:
                message_groups[group] = len(messages)
                messages.append(
                    AIMessage(
                        content="", id=_message_id(item), tool_calls=calls,
                        response_metadata=_model_call_metadata(item),
                    )
                )
            continue
        if item.semantic_kind not in {
            SemanticKind.ASSISTANT_OUTPUT,
            SemanticKind.REASONING,
        }:
            if capability_losses is not None:
                capability_losses.append(
                    f"{item.item_id}:{item.semantic_kind}/{item.payload_kind}"
                )
            continue
        group = _projection_message_group_id(item)
        content = project_reasoning_carrier(item) if item.semantic_kind == SemanticKind.REASONING else item.payload
        if content is None:
            if capability_losses is not None:
                capability_losses.append(f"{item.item_id}:{item.semantic_kind}/{item.payload_kind}")
            continue
        content = _message_content(content)
        current_index = message_groups.get(group)
        if (
            current_index != len(messages) - 1
            or current_index is None
            or not isinstance(messages[current_index], AIMessage)
        ):
            message_groups[group] = len(messages)
            messages.append(AIMessage(
                content=content, id=_message_id(item),
                response_metadata=_model_call_metadata(item),
            ))
            continue
        current = messages[current_index]
        current_content = current.content
        if isinstance(current_content, list):
            next_content: object = [
                *current_content,
                *(content if isinstance(content, list) else [content]),
            ]
        elif isinstance(content, str) and isinstance(current_content, str):
            next_content = current_content + content
        else:
            next_content = [
                current_content,
                *(content if isinstance(content, list) else [content]),
            ]
        messages[current_index] = current.model_copy(update={"content": next_content})
    return messages
