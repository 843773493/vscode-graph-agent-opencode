"""canonical item 到 LangChain 消息的无 I/O 叶映射。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.records import CanonicalItemRecord
from app.services.mapping.itemized.carrier_dedup import (
    projection_message_group_id as _projection_message_group_id,
)
from app.services.mapping.itemized.carrier_dedup import (
    superseded_stream_item_ids as _superseded_stream_item_ids,
)
from app.services.mapping.itemized.provider_request import (
    project_runtime_notice_content,
)


def _message_id(item: CanonicalItemRecord) -> str:
    value = (item.projection_identity.projection_message_id if item.projection_identity is not None else None)
    return value if isinstance(value, str) and value else item.item_id


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
                "id": str(call.get("id") or call.get("tool_call_id") or item.item_id),
                "name": str(call.get("name") or "tool"),
                "args": dict(call.get("args") or {})
                if isinstance(call.get("args"), Mapping)
                else {"raw": call.get("args", "")},
                "type": "tool_call",
            }
        )
    return result


def _item_content(item: CanonicalItemRecord) -> object:
    if item.semantic_kind != SemanticKind.REASONING:
        return item.payload
    block = {
        "type": "reasoning",
        "text": item.payload.get("content", "")
        if isinstance(item.payload, Mapping)
        else item.payload,
        "item_id": item.item_id,
    }
    return [block]


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
    superseded_ids = _superseded_stream_item_ids(ordered)
    for item in ordered:
        if item.item_id in superseded_ids:
            continue
        if item.semantic_kind == SemanticKind.REASONING and item.payload_kind in {
            "opaque",
            "extension",
        }:
            if capability_losses is not None:
                capability_losses.append(
                    f"{item.item_id}:{item.semantic_kind}/{item.payload_kind}"
                )
            continue
        if item.semantic_kind == SemanticKind.USER_INPUT:
            messages.append(
                HumanMessage(
                    content=_message_content(item.payload["content"] if isinstance(item.payload, Mapping) and "attachments" in item.payload else item.payload), id=_message_id(item)
                )
            )
            continue
        if item.semantic_kind == SemanticKind.TOOL_RESULT:
            payload = item.payload if isinstance(item.payload, Mapping) else {}
            tool_status = (
                "error"
                if payload.get("tool_outcome") in {"failure", "cancelled"}
                else "success"
            )
            messages.append(
                ToolMessage(
                    content=_message_content(payload.get("content", item.payload)),
                    id=_message_id(item),
                    tool_call_id=item.tool_result_evidence.tool_call_id if item.tool_result_evidence is not None else payload["tool_call_id"],
                    name=str(payload.get("name") or "tool"),
                    status=tool_status,
                )
            )
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
                    content=_message_content(item.payload),
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
                    AIMessage(content="", id=_message_id(item), tool_calls=calls)
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
        content = _message_content(_item_content(item))
        current_index = message_groups.get(group)
        if (
            current_index != len(messages) - 1
            or current_index is None
            or not isinstance(messages[current_index], AIMessage)
        ):
            message_groups[group] = len(messages)
            messages.append(AIMessage(content=content, id=_message_id(item)))
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
