"""将工具调用伴随的 content 保持为独立且有序的 canonical semantic items。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.domain.itemized.records import CanonicalItemRecord

_REASONING = {
    "reasoning",
    "reasoning_content",
    "reasoning_items",
    "thinking",
    "redacted_thinking",
}


def expand_tool_message(
    primary: CanonicalItemRecord,
    content: object,
) -> tuple[CanonicalItemRecord, ...]:
    if primary.semantic_kind != "tool_call":
        return (primary,)
    if not isinstance(content, (str, list)):
        raise TypeError("tool-call content 必须是 text 或有序 content list")
    payload = primary.payload if isinstance(primary.payload, Mapping) else {}
    raw_calls = payload.get("tool_calls")
    calls = (
        [call for call in raw_calls if isinstance(call, Mapping)]
        if isinstance(raw_calls, list)
        else [payload]
    )
    if len(calls) <= 1:
        if content in ("", []):
            return (primary,)
        return _expand_single_tool_message(primary, content)

    parts = [] if content in ("", []) else [content] if isinstance(content, str) else content
    content_form = "str" if isinstance(content, str) else "list"
    size = len(parts) + len(calls)
    result: list[CanonicalItemRecord] = []
    for ordinal, part in enumerate(parts):
        result.append(
            _content_item(
                primary,
                part,
                ordinal=ordinal,
                size=size,
                content_form=content_form,
            )
        )
    for call_index, call in enumerate(calls):
        ordinal = len(parts) + call_index
        metadata = {
            **primary.metadata,
            "projection_group": {
                "ordinal": ordinal,
                "size": size,
                "content_form": content_form,
            },
        }
        if ordinal != size - 1:
            metadata.pop("projection_message_id", None)
            metadata.pop("token_usage", None)
            metadata.pop("content_part_refs", None)
        item_id = (
            primary.item_id
            if ordinal == size - 1
            else f"{primary.item_id}-tool-call-{call_index}"
        )
        result.append(
            CanonicalItemRecord.create(
                item_sequence=primary.item_sequence + ordinal,
                item_id=item_id,
                semantic_kind=primary.semantic_kind,
                payload_kind=primary.payload_kind,
                status=primary.status,
                producer_ref=primary.producer_ref,
                payload={"tool_calls": [dict(call)]},
                created_at=primary.created_at,
                metadata=metadata,
                turn_id=primary.turn_id,
                turn_scope=primary.turn_scope,
                message_group_id=primary.message_group_id,
                wire_role=primary.wire_role,
            )
        )
    return tuple(result)


def _expand_single_tool_message(
    primary: CanonicalItemRecord,
    content: str | list[object],
) -> tuple[CanonicalItemRecord, ...]:
    parts = [content] if isinstance(content, str) else content
    result: list[CanonicalItemRecord] = []
    size = len(parts) + 1
    for ordinal, part in enumerate(parts):
        result.append(
            _content_item(
                primary,
                part,
                ordinal=ordinal,
                size=size,
                content_form="str" if isinstance(content, str) else "list",
            )
        )
    result.append(
        replace(
            primary,
            item_sequence=primary.item_sequence + len(parts),
            metadata={
                **primary.metadata,
                "projection_group": {
                    "ordinal": size - 1,
                    "size": size,
                    "content_form": "str" if isinstance(content, str) else "list",
                },
            },
        )
    )
    return tuple(result)


def _content_item(
    primary: CanonicalItemRecord,
    part: object,
    *,
    ordinal: int,
    size: int,
    content_form: str,
) -> CanonicalItemRecord:
    kind = "assistant_output"
    payload_kind = "text" if isinstance(part, str) else "structured_content"
    payload = part if isinstance(part, str) else [part]
    carrier: dict[str, object] = {}
    if isinstance(part, Mapping) and part.get("type") in _REASONING:
        kind = "reasoning"
        text_key = next(
            (
                key
                for key in ("reasoning", "reasoning_content", "text", "thinking")
                if isinstance(part.get(key), str)
            ),
            None,
        )
        if text_key is not None and set(part) <= {"type", text_key, "id", "index"}:
            payload_kind, payload = "text", part[text_key]
            carrier = {key: value for key, value in part.items() if key != text_key}
            carrier["text_key"] = text_key
        else:
            # 有保护字段的 carrier 使用 domain 允许的 extension envelope。
            payload_kind = "extension"
            payload = {
                "extension_schema": "boxteam.checkpoint.reasoning-carrier",
                "extension_version": "1",
                "encoding": "json",
                "wire_type": str(part["type"]),
                "schema_version": "1",
                "value": dict(part),
                "protection": {"visibility": "protected"},
            }
    elif not isinstance(part, (str, Mapping)):
        raise TypeError("tool-call content part 必须是字符串或 object")
    metadata = {
        **primary.metadata,
        "projection_group": {
            "ordinal": ordinal,
            "size": size,
            "content_form": content_form,
        },
    }
    metadata.pop("projection_message_id", None)
    # 消息级统计与 part 引用只由 anchor 持有，避免每个 companion 重复计费。
    metadata.pop("token_usage", None)
    metadata.pop("content_part_refs", None)
    if carrier:
        metadata["reasoning_carrier"] = carrier
    return CanonicalItemRecord.create(
        item_sequence=primary.item_sequence + ordinal,
        item_id=f"{primary.item_id}-content-{ordinal}",
        semantic_kind=kind,
        payload_kind=payload_kind,
        status=primary.status,
        producer_ref=primary.producer_ref,
        payload=payload,
        created_at=primary.created_at,
        metadata=metadata,
        turn_id=primary.turn_id,
        turn_scope=primary.turn_scope,
        message_group_id=primary.message_group_id,
        wire_role=primary.wire_role,
    )


def validate_group(items: Sequence[CanonicalItemRecord]) -> CanonicalItemRecord:
    if not items:
        raise ValueError("canonical message group 不得为空")
    primary = items[-1]
    manifest = primary.metadata.get("projection_group")
    if manifest is None:
        if len(items) != 1:
            raise ValueError("canonical message group 缺少显式有序 manifest")
        return primary
    if not isinstance(manifest, Mapping) or manifest.get("size") != len(items):
        raise ValueError("canonical message group 不完整")
    if primary.semantic_kind != "tool_call" or manifest.get("content_form") not in {
        "str",
        "list",
    }:
        raise ValueError("canonical message group anchor/content_form 非法")
    previous_sequence = 0
    seen_tool_call = False
    for ordinal, item in enumerate(items):
        part = item.metadata.get("projection_group")
        if (
            not isinstance(part, Mapping)
            or type(part.get("ordinal")) is not int
            or type(part.get("size")) is not int
            or dict(part) != {**manifest, "ordinal": ordinal}
            or item.message_group_id != primary.message_group_id
            or item.turn_id != primary.turn_id
            or item.turn_scope != primary.turn_scope
            or item.producer_ref != primary.producer_ref
            or item.item_sequence <= previous_sequence
        ):
            raise ValueError("canonical message group identity/order/provenance 不一致")
        if item.semantic_kind == "tool_call":
            seen_tool_call = True
        elif seen_tool_call or (
            ordinal < len(items) - 1
            and item.semantic_kind not in {"assistant_output", "reasoning"}
        ):
            raise ValueError("canonical message group 伴随 item semantic kind 非法")
        previous_sequence = item.item_sequence
    return primary


def group_content(items: Sequence[CanonicalItemRecord]) -> str | list[object]:
    primary = validate_group(items)
    result: list[object] = []
    for item in items:
        if item.semantic_kind == "tool_call":
            continue
        if item.semantic_kind == "reasoning":
            if item.payload_kind == "extension":
                if (
                    item.payload.get("extension_schema") != "boxteam.checkpoint.reasoning-carrier"
                    or item.payload.get("extension_version") != "1"
                    or not isinstance(item.payload.get("value"), Mapping)
                ):
                    raise ValueError("canonical message group reasoning extension 不受支持")
                result.append(dict(item.payload["value"]))
            else:
                carrier = dict(item.metadata["reasoning_carrier"])
                key = carrier.pop("text_key")
                result.append({**carrier, key: item.payload})
        elif item.payload_kind == "text":
            result.append(item.payload)
        else:
            result.extend(item.payload)
    if primary.metadata["projection_group"]["content_form"] == "str":
        if not result:
            return ""
        if len(result) != 1 or not isinstance(result[0], str):
            raise ValueError("canonical message group 的字符串 carrier 非法")
        return result[0]
    return result
