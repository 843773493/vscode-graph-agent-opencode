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
    original_bytes = len(value.encode("utf-8"))
    marker = (
        f"...[BoxTeam 已截断，原始字节数={original_bytes}，"
        f"sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()}]"
    )
    marker_bytes = len(marker.encode("utf-8"))
    if marker_bytes >= max_bytes:
        return marker.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    prefix = value.encode("utf-8")[: max_bytes - marker_bytes]
    return prefix.decode("utf-8", errors="ignore") + marker


def _compact(value: object, max_bytes: int) -> object:
    if max_bytes < 64:
        return "...[BoxTeam JSON payload 已截断]"
    if len(_encoded(value)) <= max_bytes:
        return value
    if isinstance(value, str):
        return _truncated_text(value, max_bytes)
    if isinstance(value, Mapping):
        items = list(value.items())
        result: dict[str, object] = {}
        child_budget = max(64, max_bytes // max(len(items), 1))
        for key, child in items:
            result[str(key)] = _compact(child, child_budget)
        while len(_encoded(result)) > max_bytes and result:
            result.pop(next(reversed(result)))
        if len(result) < len(items):
            result["__boxteam_truncated__"] = True
        if len(_encoded(result)) <= max_bytes:
            return result
        return {
            "__boxteam_truncated__": True,
            "__boxteam_original_bytes__": len(_encoded(value)),
        }
    if isinstance(value, (list, tuple)):
        child_budget = max(64, max_bytes // max(len(value), 1))
        result = [_compact(item, child_budget) for item in value]
        while len(_encoded(result)) > max_bytes and result:
            result.pop()
        if len(result) < len(value):
            result.append("...[BoxTeam list items 已截断]")
        if len(_encoded(result)) <= max_bytes:
            return result
        return {
            "__boxteam_truncated__": True,
            "__boxteam_original_bytes__": len(_encoded(value)),
        }
    return value


def bound_json_value(value: Any, *, max_bytes: int) -> object:
    """返回带明确截断标记、且序列化后不超过上限的 JSON 值。"""
    if max_bytes < 128:
        raise ValueError("JSON payload 上限至少需要 128 字节")
    encoded = _encoded(value)
    if len(encoded) <= max_bytes:
        return value
    compacted = _compact(value, max_bytes)
    if len(_encoded(compacted)) <= max_bytes:
        return compacted
    return {
        "__boxteam_truncated__": True,
        "__boxteam_original_bytes__": len(encoded),
        "__boxteam_sha256__": hashlib.sha256(encoded).hexdigest(),
    }
