"""pending 消息的无正文引用编码；canonical 映射和 I/O 由注入端口负责。"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from langchain_core.messages import (
    BaseMessage,
    RemoveMessage,
    message_to_dict,
    messages_from_dict,
)
from langgraph.types import Overwrite

MESSAGE_REFERENCE_SERIALIZER = "canonical_message_write_v1:"
_BODY_FIELDS = ("id", "content", "tool_calls", "invalid_tool_calls", "artifact", "tool_call_id")


def _difference(original: Mapping[str, object], canonical: Mapping[str, object]) -> dict[str, object]:
    """只保存框架封装字段的差异，消息正文必须能由 canonical 原样重建。"""
    changes: dict[str, object] = {}
    for key, value in original.items():
        if key not in canonical or value != canonical[key]:
            if isinstance(value, dict) and isinstance(canonical.get(key), dict):
                changes[key] = {"merge": _difference(value, canonical[key])}
            else:
                changes[key] = {"value": value}
    return {"changes": changes, "remove": sorted(canonical.keys() - original.keys())}


def _apply_difference(canonical: Mapping[str, object], patch: Mapping[str, object]) -> dict[str, object]:
    result = dict(canonical)
    if set(patch) != {"changes", "remove"} or not isinstance(patch["changes"], dict) or not isinstance(patch["remove"], list):
        raise ValueError("pending message 引用 patch 结构非法")
    for key in patch["remove"]:
        if not isinstance(key, str) or key not in result:
            raise ValueError("pending message 引用移除了不存在的字段")
        del result[key]
    for key, change in patch["changes"].items():
        if not isinstance(key, str) or not isinstance(change, dict):
            raise TypeError("pending message 引用 patch 字段非法")
        if set(change) == {"value"}:
            result[key] = change["value"]
        elif set(change) == {"merge"} and isinstance(result.get(key), dict) and isinstance(change["merge"], dict):
            result[key] = _apply_difference(result[key], change["merge"])
        else:
            raise ValueError("pending message 引用 patch 操作非法")
    return result


def reference_message_write(
    value: object,
    locate: Callable[[str], tuple[dict[str, object], Mapping[str, object]] | None],
) -> dict[str, object] | None:
    """仅当整个 batch 已有相同 canonical 正文时转引用，保留 batch/Overwrite 边界。"""
    if isinstance(value, Overwrite):
        child = reference_message_write(value.value, locate)
        return None if child is None else {"kind": "overwrite", "value": child}
    if value is None:
        return {"kind": "none"}
    if isinstance(value, (list, tuple)):
        children = [reference_message_write(message, locate) for message in value]
        if any(child is None for child in children):
            return None
        return {"kind": "tuple" if isinstance(value, tuple) else "list", "values": children}
    if isinstance(value, RemoveMessage):
        return {"kind": "remove", "value": message_to_dict(value)}
    if not isinstance(value, BaseMessage) or not isinstance(value.id, str) or not value.id:
        return None
    found = locate(value.id)
    if found is None:
        return None
    reference, canonical = found
    original = message_to_dict(value)
    original_data = original["data"]
    canonical_data = canonical.get("data")
    if not isinstance(canonical_data, dict) or original["type"] != canonical.get("type"):
        return None
    # 这些字段是消息正文；不同版本必须先走 canonical owner，不能藏入 patch。
    if any(original_data.get(key) != canonical_data.get(key) for key in _BODY_FIELDS):
        return None
    return {"kind": "message", "reference": reference, "patch": _difference(original, canonical)}


def restore_message_write(
    descriptor: Mapping[str, object],
    read: Callable[[Mapping[str, object]], Mapping[str, object]],
) -> object:
    """恢复框架原有对象，不给 Delta reducer 额外注入投影 metadata。"""
    kind = descriptor.get("kind")
    if kind == "none" and set(descriptor) == {"kind"}:
        return None
    if kind in {"list", "tuple"} and set(descriptor) == {"kind", "values"} and isinstance(descriptor["values"], list):
        children = descriptor["values"]
        if not all(isinstance(child, dict) for child in children):
            raise ValueError("pending message batch 引用非法")
        values = [restore_message_write(child, read) for child in children]
        return tuple(values) if kind == "tuple" else values
    if kind == "overwrite" and set(descriptor) == {"kind", "value"} and isinstance(descriptor["value"], dict):
        return Overwrite(restore_message_write(descriptor["value"], read))
    if kind == "remove" and set(descriptor) == {"kind", "value"} and isinstance(descriptor["value"], dict):
        value = messages_from_dict([descriptor["value"]])[0]
        if not isinstance(value, RemoveMessage):
            raise ValueError("pending message remove 控制记录非法")
        return value
    if kind == "message" and set(descriptor) == {"kind", "reference", "patch"} and isinstance(descriptor["reference"], dict) and isinstance(descriptor["patch"], dict):
        canonical = read(descriptor["reference"])
        original = _apply_difference(canonical, descriptor["patch"])
        if (original.get("type") != canonical.get("type") or not isinstance(original.get("data"), dict)
                or not isinstance(canonical.get("data"), dict)
                or any(original["data"].get(key) != canonical["data"].get(key) for key in _BODY_FIELDS)):
            raise ValueError("pending message patch 不得改写 canonical 正文")
        return messages_from_dict([original])[0]
    raise ValueError(f"pending message 引用结构非法: {kind!r}")
