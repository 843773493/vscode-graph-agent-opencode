"""checkpoint 的语义与 provenance metadata 白名单，不持久化任意 wire 字段。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from app.domain.itemized.hashing import canonical_json_bytes

SEMANTIC_FIELDS = (
    "internal",
    "phase",
    "source",
    "tool_name",
    "task_id",
    "user_initiated",
    "interrupted_at",
    "parent_session_id",
    "token_usage",
    "content_part_refs",
    "supersedes_message_id",
    "context_source_kind",
    "context_source_id",
    "context_source_name",
    "context_wire_role",
    "context_revision",
)


def semantic_metadata(response: Mapping[str, object]) -> dict[str, object]:
    nested = response.get("message_metadata")
    nested = nested if isinstance(nested, Mapping) else {}
    result: dict[str, object] = {}
    for name in SEMANTIC_FIELDS:
        value = response.get(name, nested.get(name))
        if value is None:
            continue
        if name == "internal":
            if not isinstance(value, bool):
                raise ValueError("canonical metadata.internal 必须是布尔值")
        elif name in {
            "phase",
            "source",
            "tool_name",
            "task_id",
            "interrupted_at",
            "parent_session_id",
            "supersedes_message_id",
            "context_source_kind",
            "context_source_id",
            "context_source_name",
            "context_wire_role",
            "context_revision",
        }:
            if not isinstance(value, str) or not value:
                raise ValueError(f"canonical metadata.{name} 必须是非空字符串")
            if name == "context_wire_role" and value != "user":
                raise ValueError(
                    "canonical metadata.context_wire_role 当前只能是 user"
                )
        elif name == "user_initiated":
            if not isinstance(value, bool):
                raise ValueError("canonical metadata.user_initiated 必须是布尔值")
        elif name == "token_usage" and not isinstance(value, Mapping):
            raise ValueError("canonical metadata.token_usage 必须是 object")
        elif name == "content_part_refs" and not isinstance(value, list):
            raise ValueError("canonical metadata.content_part_refs 必须是 list")
        canonical_json_bytes(value)
        result[name] = deepcopy(value)
    return result


def restore_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    """仅恢复语义字段；message_role 不是 Turn 或来源事实。"""
    result = semantic_metadata(metadata)
    provenance = {
        key: result.pop(key) for key in ("source", "parent_session_id") if key in result
    }
    if provenance:
        # canonical item 的 provenance 同时服务于两类消费者：消息 codec
        # 需要嵌套的 message_metadata，状态/执行投影仍按顶层语义字段读取。
        # 两者表达同一份已封存事实，不在这里生成第二套值。
        result.update(provenance)
        result["message_metadata"] = provenance
    return result
