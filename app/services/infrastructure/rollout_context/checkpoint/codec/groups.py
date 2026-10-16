"""将工具调用伴随的 content 保持为独立且有序的 canonical semantic items。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.domain.itemized.hashing import content_hash
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionGroup,
    ReasoningCarrier,
)
from app.domain.itemized.identity.tool_call_identity import matches_tool_call_id
from app.domain.itemized.records import (
    CanonicalItemRecord,
    parse_item_projection_manifest,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    CANONICAL_GROUP_FIELD,
    TOOL_BINDING_FIELD,
    TOOL_BINDING_KEYS,
    companion_extensions,
    message_annotations,
    project_annotations,
)
from app.services.mapping.itemized.canonical_langchain import (
    REASONING_CARRIER_TYPES,
    project_reasoning_carrier,
)


def message_projection_id(item: CanonicalItemRecord) -> str:
    """只取得临时 carrier 的展示 ID，不回填 canonical typed 身份。"""
    identity = item.projection_identity
    if identity is not None and identity.projection_message_id is not None:
        return identity.projection_message_id
    return item.item_id.removeprefix("item-")


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

    parts = (
        []
        if content in ("", [])
        else [content]
        if isinstance(content, str)
        else content
    )
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
        identity = primary.projection_identity
        if ordinal != size - 1 and identity is not None:
            identity = replace(
                identity, projection_message_id=None, content_part_refs=()
            )
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
                extensions=primary.extensions
                if ordinal == size - 1
                else companion_extensions(primary),
                projection_identity=identity,
                projection_group=ProjectionGroup(ordinal, size, content_form),
                source_manifest=primary.source_manifest,
                provenance_relations=primary.provenance_relations,
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
            projection_group=ProjectionGroup(
                size - 1,
                size,
                "str" if isinstance(content, str) else "list",
            ),
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
    if isinstance(part, Mapping) and part.get("type") in REASONING_CARRIER_TYPES:
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
    identity = primary.projection_identity or ItemProjectionIdentity()
    identity = replace(
        identity,
        projection_message_id=None,
        content_part_refs=(),
        reasoning_carrier=ReasoningCarrier.from_dict(carrier) if carrier else None,
    )
    return CanonicalItemRecord.create(
        item_sequence=primary.item_sequence + ordinal,
        item_id=f"{primary.item_id}-content-{ordinal}",
        semantic_kind=kind,
        payload_kind=payload_kind,
        status=primary.status,
        producer_ref=primary.producer_ref,
        payload=payload,
        created_at=primary.created_at,
        extensions=companion_extensions(primary),
        projection_identity=identity,
        projection_group=ProjectionGroup(ordinal, size, content_form),
        source_manifest=primary.source_manifest,
        provenance_relations=primary.provenance_relations,
        turn_id=primary.turn_id,
        turn_scope=primary.turn_scope,
        message_group_id=primary.message_group_id,
        wire_role=primary.wire_role,
    )


def validate_group(items: Sequence[CanonicalItemRecord]) -> CanonicalItemRecord:
    if not items:
        raise ValueError("canonical message group 不得为空")
    primary = items[-1]
    manifest = primary.projection_group
    if manifest is None:
        if len(items) != 1:
            raise ValueError("canonical message group 缺少显式有序 manifest")
        return primary
    if manifest.size != len(items):
        raise ValueError("canonical message group 不完整")
    if primary.semantic_kind != "tool_call" or manifest.content_form not in {
        "str",
        "list",
    }:
        raise ValueError("canonical message group anchor/content_form 非法")
    previous_sequence = 0
    seen_tool_call = False
    for ordinal, item in enumerate(items):
        part = item.projection_group
        if (
            part != replace(manifest, ordinal=ordinal)
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


def restore_group_manifests(
    items: Sequence[CanonicalItemRecord],
    response: Mapping[str, object],
) -> tuple[CanonicalItemRecord, ...]:
    """临时 carrier 携带无正文 typed 镜像，逐成员恢复并拒绝展示坐标冲突。"""
    raw = response.get(CANONICAL_GROUP_FIELD)
    if raw is None:
        return tuple(items)
    if not isinstance(raw, list) or len(raw) != len(items):
        raise ValueError("checkpoint canonical group manifest 成员数不一致")
    restored: list[CanonicalItemRecord] = []
    for item, entry in zip(items, raw, strict=True):
        if not isinstance(entry, Mapping) or entry.get("item_id") != item.item_id:
            raise ValueError("checkpoint canonical group manifest item identity 不一致")
        manifest = parse_item_projection_manifest(
            {key: value for key, value in entry.items() if key not in {"item_id", TOOL_BINDING_FIELD}}
        )
        identity = manifest["projection_identity"]
        identity = ItemProjectionIdentity.from_dict(identity) if identity is not None else None
        values = {**item.to_dict(), **manifest}
        if TOOL_BINDING_FIELD in entry:
            binding = entry[TOOL_BINDING_FIELD]
            binding_keys = TOOL_BINDING_KEYS.get(item.semantic_kind, ())
            calls = item.payload.get("tool_calls") if isinstance(item.payload, Mapping) else None
            message_call_id = (
                calls[0].get("id")
                if isinstance(calls, list) and len(calls) == 1
                else item.payload.get("tool_call_id")
                if isinstance(item.payload, Mapping) and item.semantic_kind == "tool_result"
                else None
            )
            if (
                not isinstance(binding, Mapping)
                or "tool_call_id" not in binding
                or set(binding) - set(binding_keys)
                or any(not isinstance(value, str) or not value for value in binding.values())
                or not isinstance(message_call_id, str)
                or not matches_tool_call_id(identity, message_call_id, binding["tool_call_id"])
            ):
                raise ValueError(f"checkpoint {item.semantic_kind.replace('_', '-')} binding 与消息调用身份不一致")
            # 生命周期镜像只保存 ID；正文仍完全取自消息中的真实 name/args/content。
            payload = (
                {"name": calls[0]["name"], "args": calls[0]["args"]}
                if item.semantic_kind == "tool_call"
                else {key: value for key, value in item.payload.items() if key not in binding_keys}
            )
            values["payload"] = {**payload, **binding}
            values["content_hash"] = content_hash(item.payload_kind, values["payload"])
        evidence = manifest["tool_result_evidence"]
        if evidence is not None:
            if item.semantic_kind != "tool_result" or not isinstance(
                item.payload, Mapping
            ):
                raise ValueError(
                    "checkpoint text tool_result evidence 与消息角色不一致"
                )
            if not matches_tool_call_id(
                identity, item.payload["tool_call_id"], evidence["tool_call_id"],
            ) or not isinstance(item.payload["content"], str):
                raise ValueError(
                    "checkpoint text tool_result evidence 与消息调用/正文不一致"
                )
            values["payload_kind"], values["payload"] = "text", item.payload["content"]
            values["content_hash"] = content_hash("text", values["payload"])
        candidate = CanonicalItemRecord.from_dict(values)
        if candidate.projection_group != item.projection_group:
            raise ValueError("checkpoint canonical group manifest 有序坐标不一致")
        if candidate.projection_identity is not None:
            # 只有 anchor 拥有 carrier 展示 ID；伴随成员保留空消息身份。
            # 比较副本使用展示坐标，返回的 canonical typed 身份不变。
            visible_identity = replace(
                candidate.projection_identity,
                block_id=None,
                block_index=None,
                projection_message_id=(
                    message_projection_id(candidate)
                    if item is items[-1]
                    else candidate.projection_identity.projection_message_id
                ),
            )
            if visible_identity != item.projection_identity:
                raise ValueError(
                    "checkpoint canonical projection identity 与消息展示坐标不一致"
                )
        if (
            candidate.source_manifest.source_revision
            if candidate.source_manifest
            else None
        ) != (item.source_manifest.source_revision if item.source_manifest else None):
            raise ValueError(
                "checkpoint canonical source manifest 与消息 revision 不一致"
            )
        restored.append(candidate)
    anchor = restored[-1]
    if anchor.provenance_relations != items[-1].provenance_relations:
        raise ValueError("checkpoint canonical provenance 与消息声明不一致")
    if message_annotations(project_annotations(anchor)) != message_annotations(
        response
    ):
        raise ValueError("checkpoint canonical annotations 与消息展示字段不一致")
    validate_group(restored)
    return tuple(restored)


def group_content(items: Sequence[CanonicalItemRecord]) -> str | list[object]:
    primary = validate_group(items)
    result: list[object] = []
    for item in items:
        if item.semantic_kind == "tool_call":
            continue
        if item.semantic_kind == "reasoning":
            if item.payload_kind != "extension" and (
                item.projection_identity is None
                or item.projection_identity.reasoning_carrier is None
            ):
                raise ValueError("reasoning group 缺少 typed carrier")
            carrier = project_reasoning_carrier(item)
            if carrier is None:
                raise ValueError("canonical message group reasoning extension 不受支持")
            result.append(carrier)
        elif item.payload_kind == "text":
            result.append(item.payload)
        else:
            result.extend(item.payload)
    if primary.projection_group.content_form == "str":
        if not result:
            return ""
        if len(result) != 1 or not isinstance(result[0], str):
            raise ValueError("canonical message group 的字符串 carrier 非法")
        return result[0]
    return result
