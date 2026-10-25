from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def _encoded(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _truncated_text(value: str, max_bytes: int) -> str:
    """截断文本；结果按 JSON 字符串编码后不超过 ``max_bytes``。

    ``max_bytes`` 是 JSON 形态的字节预算，所以先扣掉两侧引号再裁剪文本，
    否则截断结果会被引号顶出上限。
    """
    encoded_value = value.encode("utf-8")
    original_bytes = len(encoded_value)
    digest = hashlib.sha256(encoded_value).hexdigest()
    marker = f"...[BoxTeam 已截断，原始字节数={original_bytes}，sha256={digest}]"
    text_budget = max_bytes - 2
    encoded_marker = marker.encode("utf-8")
    marker_bytes = len(encoded_marker)
    if marker_bytes >= text_budget:
        return encoded_marker[:text_budget].decode("utf-8", errors="ignore")
    prefix = encoded_value[: text_budget - marker_bytes]
    return prefix.decode("utf-8", errors="ignore") + marker


def _truncation_fallback(serialized_source: bytes, max_bytes: int) -> object:
    """预算不足以容纳结构化标记时，退化为有界的文本截断标记。

    结构化标记固定占用百余字节，小预算下自身即超限；此时改用文本形态，
    由 :func:`_truncated_text` 保证严格落在预算内。
    """
    source_byte_count = len(serialized_source)
    source_sha256 = hashlib.sha256(serialized_source).hexdigest()
    marker = {
        "__boxteam_truncated__": True,
        "__boxteam_original_bytes__": source_byte_count,
        "__boxteam_sha256__": source_sha256,
    }
    if len(_encoded(marker)) <= max_bytes:
        return marker
    return _truncated_text(
        "...[BoxTeam JSON payload 已截断，原始字节数="
        f"{source_byte_count}，sha256={source_sha256}]",
        max_bytes,
    )


def _compact_text(
    value: str,
    max_bytes: int,
    *,
    serialized_source: bytes | None = None,
) -> tuple[object, int]:
    compacted = _truncated_text(value, max_bytes)
    compacted_byte_count = len(_encoded(compacted))
    if compacted_byte_count <= max_bytes:
        return compacted, compacted_byte_count
    source = _encoded(value) if serialized_source is None else serialized_source
    fallback = _truncation_fallback(source, max_bytes)
    return fallback, len(_encoded(fallback))


def _compact(
    value: object,
    max_bytes: int,
    *,
    serialized_source: bytes | None = None,
) -> tuple[object, int]:
    if max_bytes < 64:
        compacted = "...[BoxTeam JSON payload 已截断]"
        return compacted, len(_encoded(compacted))
    if isinstance(value, str) and len(value) + 2 > max_bytes:
        return _compact_text(value, max_bytes, serialized_source=serialized_source)
    serialized_value = (
        _encoded(value) if serialized_source is None else serialized_source
    )
    if len(serialized_value) <= max_bytes:
        return value, len(serialized_value)
    if isinstance(value, str):
        return _compact_text(value, max_bytes, serialized_source=serialized_value)
    if isinstance(value, Mapping):
        source_item_count = len(value)
        result: dict[str, object] = {}
        member_json_bytes: dict[str, tuple[int, int]] = {}
        result_json_bytes = 2
        child_budget = max(64, max_bytes // max(source_item_count, 1))
        for key, child in value.items():
            result_key = str(key)
            compacted_child, child_json_bytes = _compact(child, child_budget)
            key_json_bytes = len(_encoded(result_key))
            previous_member_bytes = member_json_bytes.get(result_key)
            if previous_member_bytes is None:
                if result:
                    result_json_bytes += 1
                result_json_bytes += key_json_bytes + 1 + child_json_bytes
            else:
                result_json_bytes += child_json_bytes - previous_member_bytes[1]
            result[result_key] = compacted_child
            member_json_bytes[result_key] = (key_json_bytes, child_json_bytes)
        while result_json_bytes > max_bytes and result:
            result_key = next(reversed(result))
            key_json_bytes, child_json_bytes = member_json_bytes.pop(result_key)
            result_json_bytes -= key_json_bytes + 1 + child_json_bytes
            if len(result) > 1:
                result_json_bytes -= 1
            result.pop(result_key)
        if len(result) < source_item_count:
            marker_key = "__boxteam_truncated__"
            marker_value_bytes = len(_encoded(True))
            previous_member_bytes = member_json_bytes.get(marker_key)
            if previous_member_bytes is None:
                if result:
                    result_json_bytes += 1
                result_json_bytes += len(_encoded(marker_key)) + 1 + marker_value_bytes
            else:
                result_json_bytes += marker_value_bytes - previous_member_bytes[1]
            result[marker_key] = True
        if result_json_bytes <= max_bytes:
            return result, result_json_bytes
        fallback = _truncation_fallback(serialized_value, max_bytes)
        return fallback, len(_encoded(fallback))
    if isinstance(value, (list, tuple)):
        source_item_count = len(value)
        child_budget = max(64, max_bytes // max(source_item_count, 1))
        result: list[object] = []
        result_json_bytes = 2
        for item in value:
            compacted_item, item_byte_count = _compact(item, child_budget)
            if result:
                result_json_bytes += 1
            result.append(compacted_item)
            result_json_bytes += item_byte_count
            if result_json_bytes > max_bytes:
                result.pop()
                result_json_bytes -= item_byte_count
                if result:
                    result_json_bytes -= 1
                break
        if len(result) < source_item_count:
            marker = "...[BoxTeam list items 已截断]"
            if result:
                result_json_bytes += 1
            result.append(marker)
            result_json_bytes += len(_encoded(marker))
        if result_json_bytes <= max_bytes:
            return result, result_json_bytes
        fallback = _truncation_fallback(serialized_value, max_bytes)
        return fallback, len(_encoded(fallback))
    return value, len(serialized_value)


def bound_json_value(value: Any, *, max_bytes: int) -> object:
    """返回带明确截断标记、且序列化后不超过上限的 JSON 值。"""
    if max_bytes < 128:
        raise ValueError("JSON payload 上限至少需要 128 字节")
    if isinstance(value, str) and len(value) + 2 > max_bytes:
        compacted, compacted_byte_count = _compact_text(value, max_bytes)
        if compacted_byte_count <= max_bytes:
            return compacted
        return _truncation_fallback(_encoded(value), max_bytes)
    serialized_value = _encoded(value)
    if len(serialized_value) <= max_bytes:
        return value
    compacted, compacted_byte_count = _compact(
        value, max_bytes, serialized_source=serialized_value
    )
    if compacted_byte_count <= max_bytes:
        return compacted
    return _truncation_fallback(serialized_value, max_bytes)
