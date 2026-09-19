"""plan 草稿与 sealed assembly 共用的 v2 registry 解析；不创建 selection binding。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import ContextContribution


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ItemSchemaError(f"{field_name} 必须是非负整数或 NULL")
    return value


def _optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ItemSchemaError(f"{field_name} 必须是正整数或 NULL")
    return value


def _required_non_negative_int(value: object, field_name: str) -> int:
    result = _optional_non_negative_int(value, field_name)
    if result is None:
        raise ItemSchemaError(f"{field_name} 必须是非负整数")
    return result


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ItemSchemaError(f"{field_name} 必须是 boolean")
    return value


def parse_context_ref(raw: Mapping[str, object]) -> ContextRef:
    if "ref_kind" in raw or "request_only" in raw:
        raise FormatDispatchError(
            "v2 ContextRef 不得持久化 ref_kind/request_only alias"
        )
    if "ref_type" not in raw:
        raise ItemSchemaError("ContextRef 缺少 ref_type")
    ref_type = raw.get("ref_type")
    if not isinstance(ref_type, str):
        raise ItemSchemaError("ContextRef 缺少 ref_type")
    required_ref_fields = {
        "session_id",
        "plan_id",
        "ref_type",
        "ref_id",
        "semantic_kind",
        "payload_kind",
        "status",
        "item_sequence",
        "source_revision",
        "content_length",
        "source_ref",
        "base_delta_role",
        "source_overlay_epoch",
        "overlay_from_revision",
        "overlay_to_revision",
        "overlay_diff_hash",
        "content_hash",
        "redacted_stable_digest",
        "visibility",
        "protection",
        "availability",
    }
    missing_ref_fields = sorted(required_ref_fields - set(raw))
    if missing_ref_fields:
        raise ItemSchemaError(
            "ContextRef 缺少 manifest 字段: " + ",".join(missing_ref_fields)
        )
    if set(raw) - required_ref_fields:
        raise ItemSchemaError("ContextRef 含未知或不属于 ref 的字段")
    content_hash_value = raw.get("content_hash")
    digest_value = raw.get("redacted_stable_digest")
    availability_value = raw["availability"]
    if not isinstance(availability_value, str):
        raise ItemSchemaError("ContextRef.availability 必须是字符串")
    available = availability_value == "available"
    if available and (content_hash_value is None) == (digest_value is None):
        raise ItemSchemaError(
            "ContextAssemblySnapshot ContextRef 必须恰好包含一个 hash token"
        )
    if available and (
        raw.get("source_revision") is None or raw.get("content_length") is None
    ):
        raise ItemSchemaError(
            "ContextAssemblySnapshot ContextRef 缺少 source manifest"
        )
    return ContextRef(
        session_id=_required_string(raw["session_id"], "ContextRef.session_id"),
        plan_id=_optional_string(raw["plan_id"], "ContextRef.plan_id"),
        ref_type=ref_type,
        ref_id=_required_string(raw["ref_id"], "ContextRef.ref_id"),
        semantic_kind=(
            _optional_string(raw.get("semantic_kind"), "ContextRef.semantic_kind")
        ),
        payload_kind=(
            _optional_string(raw.get("payload_kind"), "ContextRef.payload_kind")
        ),
        status=_optional_string(raw.get("status"), "ContextRef.status"),
        content_hash=_optional_string(
            content_hash_value, "ContextRef.content_hash"
        ),
        redacted_stable_digest=(
            _optional_string(digest_value, "ContextRef.redacted_stable_digest")
        ),
        source_revision=(
            _optional_string(
                raw.get("source_revision"), "ContextRef.source_revision"
            )
        ),
        item_sequence=_optional_positive_int(
            raw.get("item_sequence"), "ContextRef.item_sequence"
        ),
        content_length=_optional_non_negative_int(
            raw.get("content_length"), "ContextRef.content_length"
        ),
        base_delta_role=_required_string(
            raw["base_delta_role"], "ContextRef.base_delta_role"
        ),
        source_overlay_epoch=_optional_non_negative_int(
            raw["source_overlay_epoch"],
            "ContextRef.source_overlay_epoch",
        ),
        overlay_from_revision=_optional_string(
            raw["overlay_from_revision"],
            "ContextRef.overlay_from_revision",
        ),
        overlay_to_revision=_optional_string(
            raw["overlay_to_revision"], "ContextRef.overlay_to_revision"
        ),
        overlay_diff_hash=_optional_string(
            raw["overlay_diff_hash"], "ContextRef.overlay_diff_hash"
        ),
        source_ref=(
            DetailRef.from_dict(raw["source_ref"])
            if isinstance(raw["source_ref"], Mapping)
            else _optional_string(raw["source_ref"], "ContextRef.source_ref")
        ),
        visibility=_required_string(raw["visibility"], "ContextRef.visibility"),
        protection=_required_string(raw["protection"], "ContextRef.protection"),
        availability=_required_string(
            raw["availability"], "ContextRef.availability"
        ),
    )

def parse_contribution(raw: object, *, sealed: bool = True) -> ContextContribution:
    if not isinstance(raw, Mapping):
        raise ItemSchemaError("ContextAssemblySnapshot.contributions 元素非法")
    required_contribution_fields = {
        "contribution_id",
        "source_kind",
        "source_revision",
        "content_hash",
        "content_length",
        "redacted_stable_digest",
        "request_only",
        "contribution_kind",
        "visibility",
        "protection",
        "assembly_id",
        "contribution_ordinal",
        "metadata",
        "body",
    }
    missing_contribution_fields = sorted(required_contribution_fields - set(raw))
    if missing_contribution_fields:
        raise ItemSchemaError(
            "ContextContribution 缺少字段: " + ",".join(missing_contribution_fields)
        )
    # source_ordinal 是可选 typed 字段（registry 分配值随 unsealed 清单
    # 往返；sealed manifest 不携带），只加入允许集，不进入必需集。
    # root_placement 是 E1 typed 控制字段；旧 envelope 不携带时应用规范
    # 文档化默认 tail_only（默认外部内容恒为 tail_only），不是旧别名兼容。
    allowed_contribution_fields = required_contribution_fields | {
        "source_ordinal",
        "root_placement",
    }
    if set(raw) - allowed_contribution_fields:
        raise ItemSchemaError("ContextContribution 含未知或不属于 registry 的字段")
    if not isinstance(raw["request_only"], bool):
        raise ItemSchemaError("ContextContribution.request_only 必须是 boolean")
    if sealed and (raw["assembly_id"] is None or raw["contribution_ordinal"] is None):
        raise ItemSchemaError(
            "sealed ContextContribution 必须带 assembly_id/contribution_ordinal"
        )
    if not isinstance(raw["metadata"], Mapping):
        raise ItemSchemaError("ContextContribution.metadata 必须是 object")
    # source_ordinal 是 draft/source manifest 的可选 typed 字段；registry
    # 分配值随 unsealed 清单往返，sealed manifest 不携带（保持旧字节合同）。
    raw_source_ordinal = raw.get("source_ordinal")
    if raw_source_ordinal is not None and (
        not isinstance(raw_source_ordinal, int)
        or isinstance(raw_source_ordinal, bool)
        or raw_source_ordinal < 0
    ):
        raise ItemSchemaError(
            "ContextContribution.source_ordinal 必须是非负整数或 NULL"
        )
    raw_root_placement = raw.get("root_placement", "tail_only")
    if raw_root_placement not in ("root_eligible", "tail_only"):
        raise ItemSchemaError(
            f"未知 ContextContribution.root_placement: {raw_root_placement!r}"
        )
    return ContextContribution(
        contribution_id=_required_string(
            raw["contribution_id"], "ContextContribution.contribution_id"
        ),
        source_kind=_required_string(
            raw["source_kind"], "ContextContribution.source_kind"
        ),
        source_revision=_required_string(
            raw["source_revision"], "ContextContribution.source_revision"
        ),
        content_hash=_optional_string(
            raw["content_hash"], "ContextContribution.content_hash"
        ),
        request_only=raw["request_only"],
        metadata=dict(raw["metadata"]),
        contribution_kind=_required_string(
            raw["contribution_kind"], "ContextContribution.contribution_kind"
        ),
        visibility=_required_string(
            raw["visibility"], "ContextContribution.visibility"
        ),
        protection=_required_string(
            raw["protection"], "ContextContribution.protection"
        ),
        body=raw.get("body"),
        content_length=_required_non_negative_int(
            raw["content_length"], "ContextContribution.content_length"
        ),
        redacted_stable_digest=_optional_string(
            raw["redacted_stable_digest"],
            "ContextContribution.redacted_stable_digest",
        ),
        assembly_id=_optional_string(
            raw["assembly_id"], "ContextContribution.assembly_id"
        ),
        contribution_ordinal=_optional_non_negative_int(
            raw["contribution_ordinal"],
            "ContextContribution.contribution_ordinal",
        ),
        source_ordinal=raw_source_ordinal,
        root_placement=raw_root_placement,
    )


def parse_tool_set_ref(tool: object) -> ToolSetRef:
    if not isinstance(tool, Mapping):
        raise ItemSchemaError("ContextAssemblySnapshot.tool_set_refs 元素非法")
    if set(tool) & {"ref_kind", "request_only"}:
        raise FormatDispatchError("ToolSetRef 禁止 ref_kind/request_only alias")
    if tool.get("ref_type") != "tool_set":
        raise ItemSchemaError("ToolSetRef.ref_type 必须是 tool_set")
    if not isinstance(tool.get("tool_policy"), Mapping):
        raise ItemSchemaError("ToolSetRef.tool_policy 必须是 object")
    raw_tool_values = tool.get("tools")
    if not isinstance(raw_tool_values, (list, tuple)) or not all(
        isinstance(item, Mapping) for item in raw_tool_values
    ):
        raise ItemSchemaError("ToolSetRef.tools 必须是 object array")
    missing_tool_fields = sorted(
        {
            "session_id",
            "ref_type",
            "ref_id",
            "plan_id",
            "assembly_id",
            "source_revision",
            "tool_set_schema",
            "tool_set_schema_version",
            "tool_policy_version",
            "content_length",
            "content_hash",
            "redacted_stable_digest",
            "protection",
            "availability",
            "tool_policy",
            "tools",
        }
        - set(tool)
    )
    if missing_tool_fields:
        raise ItemSchemaError(
            "ToolSetRef 缺少字段: " + ",".join(missing_tool_fields)
        )
    if set(tool) - {
        "session_id", "ref_type", "ref_id", "plan_id", "assembly_id", "source_revision",
        "tool_set_schema", "tool_set_schema_version", "tool_policy_version", "content_length",
        "content_hash", "redacted_stable_digest", "protection", "availability", "tool_policy", "tools",
    }:
        raise ItemSchemaError("ToolSetRef 含未知或不属于 registry 的字段")
    return ToolSetRef(
        session_id=_required_string(tool["session_id"], "ToolSetRef.session_id"),
        ref_id=_required_string(tool["ref_id"], "ToolSetRef.ref_id"),
        plan_id=_required_string(tool["plan_id"], "ToolSetRef.plan_id"),
        source_revision=_required_string(
            tool["source_revision"], "ToolSetRef.source_revision"
        ),
        tool_set_schema=_required_string(
            tool["tool_set_schema"], "ToolSetRef.tool_set_schema"
        ),
        tool_set_schema_version=_required_string(
            tool["tool_set_schema_version"],
            "ToolSetRef.tool_set_schema_version",
        ),
        tool_policy_version=_required_string(
            tool["tool_policy_version"], "ToolSetRef.tool_policy_version"
        ),
        content_length=_required_non_negative_int(
            tool["content_length"], "ToolSetRef.content_length"
        ),
        content_hash=_optional_string(
            tool["content_hash"], "ToolSetRef.content_hash"
        ),
        redacted_stable_digest=_optional_string(
            tool["redacted_stable_digest"],
            "ToolSetRef.redacted_stable_digest",
        ),
        protection=_required_string(tool["protection"], "ToolSetRef.protection"),
        availability=_required_string(
            tool["availability"], "ToolSetRef.availability"
        ),
        tool_policy=dict(tool["tool_policy"]),
        tools=tuple(dict(item) for item in tool["tools"]),
        assembly_id=(
            _optional_string(tool["assembly_id"], "ToolSetRef.assembly_id")
            if tool.get("assembly_id") is not None
            else None
        ),
    )
