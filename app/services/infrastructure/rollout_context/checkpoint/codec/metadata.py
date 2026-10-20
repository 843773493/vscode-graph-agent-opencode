"""checkpoint 展示 annotation；核心身份由 typed 字段独占。"""

from collections.abc import Mapping
from copy import deepcopy

from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import (
    GENERATED_RESULT_ANNOTATION_FIELDS,
    CanonicalItemRecord,
)
from app.domain.itemized.schema import validate_extensions

ANNOTATION_SCHEMA = "boxteam.checkpoint.annotations"
CANONICAL_GROUP_FIELD = "canonical_item_manifests"
TOOL_BINDING_FIELD = "tool_binding"
TOOL_BINDING_KEYS = {
    "tool_call": ("tool_call_id", "tool_invocation_id", "tool_attempt_id"),
    "tool_result": ("tool_call_id", "tool_invocation_id", "tool_attempt_id", "result_id"),
}
# 普通消息的既有控制字段保留原合同；生成结果只接受闭合的结构标记。
ANNOTATION_FIELDS = (
    "structured_prompt_kind", "structured_prompt_schema_version",
    "boxteam_session_provider_id",
    "goal_id", "goal_continuation", "goal_objective", "goal_budget_limited", "goal_objective_updated",
    "team_id", "team_task_id", "coordinator_thread_id", "member_thread_id",
    "boxteam_generation_run_id", "boxteam_generator_id", "boxteam_generation_phase",
    "attempt", "missing_tools", "source_address", "target", "communication_id", "kind", "reply_required", "reply_to_communication_id",
    "source",
    "tool_name",
    "task_id",
    "interrupted_at",
    "parent_session_id",
    "token_usage",
)


def message_annotations(response: Mapping[str, object]) -> dict[str, object]:
    extensions = deepcopy(response.get("extensions", {}))
    validate_extensions(extensions)
    nested = response.get("message_metadata")
    nested = nested if isinstance(nested, Mapping) else {}
    generated_result = response.get("structured_prompt_kind", nested.get("structured_prompt_kind")) == "generated_session_result"
    if generated_result:
        forbidden = (set(ANNOTATION_FIELDS) - GENERATED_RESULT_ANNOTATION_FIELDS) & (set(response) | set(nested))
        if forbidden:
            raise ValueError("generated_session_result metadata 含控制字段: " + ",".join(sorted(forbidden)))
    value = {}
    for name in (GENERATED_RESULT_ANNOTATION_FIELDS if generated_result else ANNOTATION_FIELDS):
        child = response.get(name, nested.get(name))
        if child is not None:
            canonical_json_bytes(child)
            value[name] = deepcopy(child)
    if value:
        existing = extensions.get(ANNOTATION_SCHEMA)
        if existing is not None:
            if existing["extension_version"] != "1" or not isinstance(
                existing["value"], Mapping
            ):
                raise ValueError("checkpoint annotations 与 producer 展示字段版本冲突")
            value = {**existing["value"], **value}
        extensions[ANNOTATION_SCHEMA] = {
            "extension_version": "1",
            "protection": "public",
            "value": value,
        }
    return extensions


def companion_extensions(primary: CanonicalItemRecord) -> dict[str, object]:
    """整次消息的 token_usage 只属于 group anchor；未知扩展保持原样。"""
    result = deepcopy(dict(primary.extensions))
    envelope = result.get(ANNOTATION_SCHEMA)
    if isinstance(envelope, Mapping) and envelope["extension_version"] == "1":
        value = envelope["value"]
        if not isinstance(value, Mapping):
            raise TypeError("checkpoint annotations value 必须是 object")
        result[ANNOTATION_SCHEMA] = {
            **envelope,
            "value": {
                key: child for key, child in value.items() if key != "token_usage"
            },
        }
        if not result[ANNOTATION_SCHEMA]["value"]:
            del result[ANNOTATION_SCHEMA]
    return result


def producer_response_metadata(response: Mapping[str, object]) -> dict[str, object]:
    """新消息保留显式扩展，移除只属于旧 canonical group 的临时镜像。"""
    result = deepcopy(dict(response))
    manifests = result.pop(CANONICAL_GROUP_FIELD, None)
    if manifests is not None:
        if not isinstance(manifests, list) or not manifests:
            raise ValueError("checkpoint canonical group manifest 必须是非空有序 array")
        result["extensions"] = deepcopy(manifests[-1]["extensions"])
        validate_extensions(result["extensions"])
    return result


def project_annotations(item: CanonicalItemRecord) -> dict[str, object]:
    """只恢复已知版本的展示字段，不把未知扩展交给消息/控制消费者。"""
    envelope = item.extensions.get(ANNOTATION_SCHEMA)
    display = item.payload.get("display_part") if isinstance(item.payload, Mapping) else None
    generated_result = isinstance(display, Mapping) and display.get("kind") == "generated_session_result"
    if not isinstance(envelope, Mapping) or envelope["extension_version"] != "1":
        return {}
    value = envelope["value"]
    if not isinstance(value, Mapping):
        raise TypeError("checkpoint annotations value 必须是 object")
    result = {
        name: deepcopy(value[name])
        for name in (GENERATED_RESULT_ANNOTATION_FIELDS if generated_result else ANNOTATION_FIELDS)
        if name in value
    }
    provenance = {
        key: result[key]
        for key in ("source", "parent_session_id", "source_address", "target", "communication_id", "kind", "reply_required", "reply_to_communication_id")
        if key in result
    }
    if provenance:
        result["message_metadata"] = provenance
    return result
