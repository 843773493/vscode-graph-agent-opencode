"""itemized v2 schema 的闭合集合与兼容矩阵。

本模块只包含 provider-neutral 的值域和组合校验，不访问 SQLite、JSONL、
LangChain 或 provider。所有 v2 reader/writer 必须使用这里的矩阵，避免
不同投影器各自解释 semantic/payload/status。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.enums import (
    BaseDeltaRole,
    CanonicalItemStatus,
    PayloadKind,
    SelectionKind,
    SemanticKind,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import _ensure_json_value

CORE_FIELDS = (
    "format_version",
    "record_type",
    "item_sequence",
    "item_id",
    "semantic_kind",
    "payload_kind",
    "status",
    "producer_ref",
    "payload",
    "content_hash",
    "created_at",
    "metadata",
)

ITEM_STATUSES = frozenset(item.value for item in CanonicalItemStatus)
PAYLOAD_KINDS = frozenset(item.value for item in PayloadKind)
SEMANTIC_KINDS = frozenset(item.value for item in SemanticKind)
TOOL_OUTCOMES = frozenset({"success", "failure", "cancelled", "unknown"})
PRODUCER_KINDS = frozenset(
    {"user", "provider", "middleware", "tool", "system", "runtime"}
)
PROVENANCE_RELATIONS = frozenset(
    {
        "influenced_by",
        "transformed_by",
        "derived_from",
        "summary_of",
        "replaces",
        "notice_for",
        "causes",
        "result_of",
        "retry_of",
        "resumes",
        "replay_input",
        "produced_by",
    }
)
VISIBILITIES = frozenset({"public", "internal", "private"})
PROTECTIONS = frozenset({"public", "redacted", "protected"})
AVAILABILITIES = frozenset({"available", "unavailable", "forbidden", "expired"})

SELECTION_COMPATIBILITY: dict[str, tuple[str, str]] = {
    SelectionKind.CANONICAL_HISTORY: ("canonical_item", BaseDeltaRole.NONE),
    SelectionKind.REQUEST_ONLY: ("request_only", BaseDeltaRole.NONE),
    SelectionKind.OVERLAY_BASE: ("request_only", BaseDeltaRole.BASE),
    SelectionKind.OVERLAY_DELTA: ("request_only", BaseDeltaRole.DELTA),
    SelectionKind.TOOL_SET: ("tool_set", BaseDeltaRole.NONE),
}

ALLOWED_PAYLOADS: dict[str, frozenset[str]] = {
    SemanticKind.USER_INPUT: frozenset(
        {PayloadKind.TEXT, PayloadKind.STRUCTURED_CONTENT}
    ),
    SemanticKind.ASSISTANT_OUTPUT: frozenset(
        {PayloadKind.TEXT, PayloadKind.STRUCTURED_CONTENT}
    ),
    SemanticKind.REASONING: frozenset(
        {PayloadKind.TEXT, PayloadKind.SUMMARY, PayloadKind.OPAQUE, PayloadKind.EXTENSION}
    ),
    SemanticKind.TOOL_CALL: frozenset(
        {PayloadKind.TOOL_CALL, PayloadKind.STRUCTURED_CONTENT}
    ),
    SemanticKind.TOOL_RESULT: frozenset(
        {
            PayloadKind.TEXT,
            PayloadKind.STRUCTURED_CONTENT,
            PayloadKind.TOOL_RESULT,
            PayloadKind.OPAQUE,
            PayloadKind.EXTENSION,
        }
    ),
    SemanticKind.RUNTIME_NOTICE: frozenset(
        {PayloadKind.TEXT, PayloadKind.STRUCTURED_CONTENT, PayloadKind.OPAQUE, PayloadKind.EXTENSION}
    ),
    SemanticKind.COMPACTION_SUMMARY: frozenset(
        {PayloadKind.SUMMARY, PayloadKind.STRUCTURED_CONTENT}
    ),
    SemanticKind.ATTACHMENT: frozenset({PayloadKind.ATTACHMENT_REF}),
    SemanticKind.EXTENSION: frozenset({PayloadKind.EXTENSION, PayloadKind.OPAQUE}),
}

ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    SemanticKind.USER_INPUT: frozenset({CanonicalItemStatus.COMPLETED}),
    SemanticKind.COMPACTION_SUMMARY: frozenset({CanonicalItemStatus.COMPLETED}),
    SemanticKind.RUNTIME_NOTICE: frozenset({CanonicalItemStatus.COMPLETED}),
    SemanticKind.ATTACHMENT: frozenset({CanonicalItemStatus.COMPLETED}),
}
for _semantic in (
    SemanticKind.ASSISTANT_OUTPUT,
    SemanticKind.REASONING,
    SemanticKind.TOOL_CALL,
    SemanticKind.TOOL_RESULT,
    SemanticKind.EXTENSION,
):
    ALLOWED_STATUSES[_semantic] = ITEM_STATUSES


def validate_item_compatibility(
    semantic_kind: str,
    payload_kind: str,
    status: str,
) -> None:
    """校验完整 semantic/payload/status 三元组。"""
    for name, value, allowed in (
        ("semantic_kind", semantic_kind, SEMANTIC_KINDS),
        ("payload_kind", payload_kind, PAYLOAD_KINDS),
        ("status", status, ITEM_STATUSES),
    ):
        if not isinstance(value, str):
            raise ItemSchemaError(f"{name} 必须是字符串")
        if value not in allowed:
            raise ItemSchemaError(f"未知 {name}: {value}")
    if (
        payload_kind not in ALLOWED_PAYLOADS[semantic_kind]
        or status not in ALLOWED_STATUSES[semantic_kind]
    ):
        raise ItemSchemaError(
            f"item-schema-incompatible: {semantic_kind}/{payload_kind}/{status}"
        )


def validate_selection_compatibility(
    selection_kind: str,
    ref_type: str,
    base_delta_role: str,
) -> None:
    """在 source lookup 前校验 selection tagged-union 矩阵。"""
    for name, value in (
        ("selection_kind", selection_kind),
        ("ref_type", ref_type),
        ("base_delta_role", base_delta_role),
    ):
        if not isinstance(value, str):
            raise ItemSchemaError(f"plan-order-integrity: {name} 必须是字符串")
    expected = SELECTION_COMPATIBILITY.get(selection_kind)
    if expected is None:
        raise ItemSchemaError(f"plan-order-integrity: 未知 selection_kind: {selection_kind}")
    expected_ref_type, expected_role = expected
    if (ref_type, base_delta_role) != (expected_ref_type, expected_role):
        raise ItemSchemaError(
            "plan-order-integrity: selection/ref_type/base_delta_role 不兼容: "
            f"{selection_kind}/{ref_type}/{base_delta_role}"
        )


def validate_producer_ref(value: object) -> dict[str, object]:
    """校验并复制单一 producer reference。"""
    if not isinstance(value, Mapping):
        raise ItemSchemaError("producer_ref 必须是 object")
    result = dict(value)
    allowed_fields = {
        "producer_kind",
        "producer_id",
        "invocation_id",
        "source_version",
        "source_hash",
    }
    unknown_fields = sorted(set(result) - allowed_fields)
    if unknown_fields:
        raise ItemSchemaError(
            "producer_ref 含未知字段: " + ",".join(unknown_fields)
        )
    for name in ("producer_kind", "producer_id"):
        if not isinstance(result.get(name), str) or not result[name]:
            raise ItemSchemaError(f"producer_ref.{name} 必须是非空字符串")
    if result["producer_kind"] not in PRODUCER_KINDS:
        raise ItemSchemaError(
            f"未知 producer_ref.producer_kind: {result['producer_kind']}"
        )
    for name in ("invocation_id", "source_version", "source_hash"):
        child = result.get(name)
        if child is not None and not isinstance(child, str):
            raise ItemSchemaError(f"producer_ref.{name} 必须是字符串")
    _ensure_json_value(result, "producer_ref")
    return result


def validate_extension_payload(payload: object, *, payload_kind: str) -> None:
    """校验 opaque/extension envelope，未知 extension 交给显式 adapter。"""
    if not isinstance(payload, Mapping):
        raise ItemSchemaError(f"{payload_kind} payload 必须是 object")
    for name in ("encoding", "wire_type", "schema_version"):
        if not isinstance(payload.get(name), str) or not payload[name]:
            raise ItemSchemaError(f"payload.{name} 必须是非空字符串")
    if "value" not in payload or payload["value"] is None:
        raise ItemSchemaError("payload.value 必须存在且非 NULL")
    _ensure_json_value(payload["value"], "payload.value")
    if payload_kind == PayloadKind.EXTENSION:
        for name in ("extension_schema", "extension_version"):
            if not isinstance(payload.get(name), str) or not payload[name]:
                raise ItemSchemaError(f"payload.{name} 必须是非空字符串")
