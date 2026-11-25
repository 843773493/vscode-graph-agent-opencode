"""将模型消息的语义内容和调用写为独立、有序的 canonical Item。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, replace

from app.domain.itemized.hashing import canonical_json_bytes, content_hash
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProjectionManifest,
    ProjectionGroup,
)
from app.domain.itemized.identity.tool_call_identity import matches_tool_call_id
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.codec.metadata import (
    CANONICAL_GROUP_FIELD,
    TOOL_BINDING_FIELD,
    TOOL_BINDING_KEYS,
    companion_extensions,
    message_annotations,
    project_annotations,
)
from app.services.mapping.itemized.canonical_langchain import (
    project_reasoning_carrier,
)
from app.services.mapping.itemized.content_blocks import semantic_content_payload


def message_projection_id(item: CanonicalItemRecord) -> str:
    """只取得临时 carrier 的展示 ID，不回填 canonical typed 身份。"""
    identity = item.projection_identity
    if identity is not None and identity.projection_message_id is not None:
        return identity.projection_message_id
    return item.item_id.removeprefix("item-")


def expand_message(
    primary: CanonicalItemRecord,
    content: object,
    *, tool_calls: Sequence[Mapping[str, object]],
    invalid_tool_calls: Sequence[Mapping[str, object]],
) -> tuple[CanonicalItemRecord, ...]:
    """普通输出和工具声明共用语义拆分，回执与用户输入保持原子。"""
    if primary.semantic_kind not in {"assistant_output", "tool_call"}:
        return (primary,)
    if not isinstance(content, (str, list)):
        raise TypeError("assistant content 必须是 text 或有序 content list")
    calls = tool_calls
    parts = ([] if content in ("", []) else [content] if isinstance(content, str) else content)
    size = len(parts) + len(calls)
    content_form = "str" if isinstance(content, str) else "list"
    result = [
        _content_item(primary, part, ordinal=ordinal, size=size, content_form=content_form)
        for ordinal, part in enumerate(parts)
    ]
    for call_index, call in enumerate(calls):
        if not isinstance(call, Mapping):
            raise TypeError("tool_calls 成员必须是 object")
        ordinal = len(parts) + call_index
        identity = primary.projection_identity
        if ordinal != size - 1 and identity is not None:
            identity = replace(identity, projection_message_id=None, content_part_refs=())
        result.append(CanonicalItemRecord.create(
            item_sequence=primary.item_sequence + ordinal,
            item_id=primary.item_id if ordinal == size - 1 else f"{primary.item_id}-tool-call-{call_index}",
            semantic_kind="tool_call", payload_kind=primary.payload_kind, status=primary.status,
            producer_ref=primary.producer_ref,
            payload={"tool_calls": [dict(call)]},
            created_at=primary.created_at,
            extensions=primary.extensions if ordinal == size - 1 else companion_extensions(primary),
            projection_identity=identity,
            projection_group=ProjectionGroup(ordinal, size, content_form)
            if size > 1 or content_form == "list" else None,
            source_manifest=primary.source_manifest, provenance_relations=primary.provenance_relations,
            turn_id=primary.turn_id, turn_scope=primary.turn_scope,
            message_group_id=primary.message_group_id, wire_role=primary.wire_role,
        ))
    if not result:
        result.append(primary)
    if invalid_tool_calls:
        # 无效声明属于原消息的正文协议载荷，不进入无正文的 projection manifest。
        anchor = result[-1]
        if anchor.semantic_kind == "tool_call" or anchor.payload_kind == "extension":
            payload = {**anchor.payload, "invalid_tool_calls": list(invalid_tool_calls)}
            payload_kind = anchor.payload_kind
        elif anchor.semantic_kind == "reasoning":
            carrier = project_reasoning_carrier(anchor)
            payload_kind = "extension"
            payload = {
                "extension_schema": "boxteam.checkpoint.reasoning-carrier", "extension_version": "1",
                "encoding": "json", "schema_version": "1", "wire_type": carrier["type"],
                "value": carrier, "protection": {"visibility": "protected"},
                "invalid_tool_calls": list(invalid_tool_calls),
            }
        else:
            payload_kind = "structured_content"
            payload = {"content": anchor.payload, "invalid_tool_calls": list(invalid_tool_calls)}
        result[-1] = replace(anchor, payload=payload, payload_kind=payload_kind,
                             content_hash=content_hash(payload_kind, payload))
    return tuple(result)


def _content_item(
    primary: CanonicalItemRecord,
    part: object,
    *,
    ordinal: int,
    size: int,
    content_form: str,
) -> CanonicalItemRecord:
    kind, payload_kind, payload, carrier = semantic_content_payload(part)
    if size == 1 and content_form == "list" and isinstance(part, str):
        payload_kind, payload = "structured_content", [part]
    identity = primary.projection_identity or ItemProjectionIdentity()
    identity = replace(
        identity,
        projection_message_id=identity.projection_message_id if ordinal == size - 1 else None,
        content_part_refs=identity.content_part_refs if ordinal == size - 1 else (),
        reasoning_carrier=carrier,
    )
    return CanonicalItemRecord.create(
        item_sequence=primary.item_sequence + ordinal,
        item_id=primary.item_id if ordinal == size - 1 else f"{primary.item_id}-content-{ordinal}",
        semantic_kind=kind,
        payload_kind=payload_kind,
        status=primary.status,
        producer_ref=primary.producer_ref,
        payload=payload,
        created_at=primary.created_at,
        extensions=primary.extensions if ordinal == size - 1 else companion_extensions(primary),
        projection_identity=identity,
        projection_group=ProjectionGroup(ordinal, size, content_form) if size > 1 else None,
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
    if primary.semantic_kind not in {"assistant_output", "reasoning", "tool_call"}:
        raise ValueError("canonical message group anchor/content_form 非法")
    previous_sequence = 0
    for ordinal, item in enumerate(items):
        part = item.projection_group
        if (
            part != replace(manifest, ordinal=ordinal)
            or item.message_group_id != primary.message_group_id
            or item.turn_id != primary.turn_id
            or item.turn_scope != primary.turn_scope
            or item.producer_ref != primary.producer_ref
            or item.wire_role != primary.wire_role
            or item.item_sequence <= previous_sequence
        ):
            raise ValueError("canonical message group identity/order/provenance 不一致")
        if item.semantic_kind not in {"assistant_output", "reasoning", "tool_call"}:
            raise ValueError("canonical message group 成员 semantic kind 非法")
        if (item.projection_identity.model_call_id if item.projection_identity else None) != (
            primary.projection_identity.model_call_id if primary.projection_identity else None
        ):
            raise ValueError("canonical message group ModelCall 不一致")
        previous_sequence = item.item_sequence
    return primary


def restore_group_manifests(
    items: Sequence[CanonicalItemRecord],
    response: Mapping[str, object],
) -> tuple[CanonicalItemRecord, ...]:
    """临时 carrier 携带无正文 typed 镜像，逐成员恢复并拒绝展示坐标冲突。"""
    raw = response.get(CANONICAL_GROUP_FIELD)
    if raw is None:
        validate_group(items)
        return tuple(items)
    if not isinstance(raw, list) or len(raw) != len(items):
        raise ValueError("checkpoint canonical group manifest 成员数不一致")
    restored: list[CanonicalItemRecord] = []
    for item, entry in zip(items, raw, strict=True):
        if not isinstance(entry, Mapping) or entry.get("item_id") != item.item_id:
            raise ValueError("checkpoint canonical group manifest item identity 不一致")
        manifest = ItemProjectionManifest.from_dict(
            {key: value for key, value in entry.items() if key not in {"item_id", TOOL_BINDING_FIELD}}
        )
        identity = manifest.projection_identity
        values = {field.name: getattr(manifest, field.name) for field in fields(manifest)
                  if field.name not in {"tool_execution", "tool_outcome", "output_block_type"}}
        values["payload"] = item.payload
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
                {"name": calls[0]["name"], "args": calls[0]["args"],
                 **({"invalid_tool_calls": item.payload["invalid_tool_calls"]}
                    if "invalid_tool_calls" in item.payload else {})}
                if item.semantic_kind == "tool_call"
                else {key: value for key, value in item.payload.items() if key not in binding_keys}
            )
            values["payload"] = {**payload, **binding}
            values["content_hash"] = content_hash(item.payload_kind, values["payload"])
        if manifest.tool_execution is not None:
            if item.semantic_kind != "tool_result" or not isinstance(values["payload"], Mapping):
                raise ValueError("checkpoint 工具执行证据只属于结构化 tool_result")
            # 轻量 manifest 是原结果 payload 的镜像，不是 canonical 顶层字段。
            values["payload"] = {**values["payload"], "tool_execution": manifest.tool_execution.to_dict()}
            values["content_hash"] = content_hash(item.payload_kind, values["payload"])
        if manifest.tool_outcome is not None:
            if item.semantic_kind != "tool_result" or not isinstance(values["payload"], Mapping):
                raise ValueError("checkpoint 结果 marker 只属于结构化 tool_result")
            values["payload"] = {**values["payload"], "tool_outcome": manifest.tool_outcome}
            values["content_hash"] = content_hash(item.payload_kind, values["payload"])
        evidence = manifest.tool_result_evidence
        if evidence is not None:
            if item.semantic_kind != "tool_result" or not isinstance(
                item.payload, Mapping
            ):
                raise ValueError(
                    "checkpoint text tool_result evidence 与消息角色不一致"
                )
            if not matches_tool_call_id(
                identity, item.payload["tool_call_id"], evidence.tool_call_id,
            ) or not isinstance(item.payload["content"], str):
                raise ValueError(
                    "checkpoint text tool_result evidence 与消息调用/正文不一致"
                )
            values["payload_kind"], values["payload"] = "text", item.payload["content"]
            values["content_hash"] = content_hash("text", values["payload"])
        # 已解析的 typed 字段直接参与比较和 replace，不再字典往返或重复构造。
        # 镜像与正文未变时复用候选；自由扩展仍按 JCS 比较，不能把布尔值当作数字。
        candidate = (
            item
            if values["payload"] is item.payload
            and all(getattr(item, name) == value for name, value in values.items())
            and canonical_json_bytes(manifest.extensions) == canonical_json_bytes(item.extensions)
            else replace(item, **values)
        )
        if candidate.projection_group != item.projection_group:
            raise ValueError("checkpoint canonical group manifest 有序坐标不一致")
        if candidate.output_block_type != manifest.output_block_type:
            raise ValueError("checkpoint canonical 非文本输出类型与正文不一致")
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
