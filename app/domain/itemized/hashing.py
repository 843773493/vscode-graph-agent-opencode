"""RFC 8785 JCS v1 hash primitives。"""

from __future__ import annotations

import hashlib
import math
import sys
from collections.abc import Mapping

import rfc8785

from app.domain.itemized.errors import ItemSchemaError


def _ensure_json_value(value: object, path: str = "value") -> None:
    if isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ItemSchemaError(f"{path!r} 包含非法 Unicode surrogate")
    if isinstance(value, float) and not math.isfinite(value):
        raise ItemSchemaError(f"{path} 包含非有限浮点数")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ItemSchemaError(f"{path} object key 必须是字符串")
            _ensure_json_value(key, f"{path} object key")
            _ensure_json_value(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _ensure_json_value(child, f"{path}[{index}]")
        return
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        return
    raise ItemSchemaError(f"{path} 不是 JSON value: {type(value).__name__}")


def _jcs_input(value: object) -> object:
    """适配 Python 整数与只支持安全整数的 JCS 库，保持恢复可重复。

    大整数仅允许精确 binary64 值或该值的 JCS 十进制表示（JSON 解码会
    把后者恢复成 int）。其它高精度整数必须由调用方显式编码为字符串，
    不能默默舍入。数字编码仍完全由 RFC 8785 实现负责。
    """
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        if abs(value) > int(sys.float_info.max):
            raise ItemSchemaError("整数超出 IEEE-754 有限数字范围")
        number = float(value)
        if not math.isfinite(number):
            raise ItemSchemaError("整数超出 IEEE-754 有限数字范围")
        if int(number) != value and rfc8785.dumps(number).decode("utf-8") != str(value):
            raise ItemSchemaError("整数不能无损映射到 JCS/IEEE-754，须编码为字符串")
        return number
    if isinstance(value, Mapping):
        return {key: _jcs_input(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jcs_input(child) for child in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    """返回 RFC 8785 JCS 的无空白 UTF-8 字节。"""
    _ensure_json_value(value)
    try:
        return rfc8785.dumps(_jcs_input(value))
    except (
        rfc8785.CanonicalizationError,
        rfc8785.FloatDomainError,
        rfc8785.IntegerDomainError,
    ) as error:
        raise ItemSchemaError(f"RFC 8785 JCS 无法编码 value: {error}") from error


def sha256_jcs(value: object) -> str:
    return "sha256:jcs:v1:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def content_hash(payload_kind: str, payload: object) -> str:
    if not isinstance(payload_kind, str) or not payload_kind:
        raise ItemSchemaError("payload_kind 必须是非空字符串")
    return sha256_jcs({"payload_kind": payload_kind, "payload": payload})


def payload_content_length(payload_kind: str, payload: object) -> int:
    """返回协议定义的 payload 正文长度。

    文本的长度是原始 UTF-8 正文长度；结构化 payload 使用其 JCS UTF-8
    编码长度。它与 JSONL 行长度、Python ``len(str(...))`` 和 wire message
    长度都不同，必须由所有 ref/catalog/projector 共享。
    """
    if not isinstance(payload_kind, str) or not payload_kind:
        raise ItemSchemaError("payload_kind 必须是非空字符串")
    if payload_kind == "text":
        if not isinstance(payload, str):
            raise ItemSchemaError("text payload 必须是字符串")
        _ensure_json_value(payload)
        return len(payload.encode("utf-8"))
    return len(canonical_json_bytes(payload))


def contribution_content_hash(contribution_kind: str, body: object) -> str:
    if not isinstance(contribution_kind, str) or not contribution_kind:
        raise ItemSchemaError("contribution_kind 必须是非空字符串")
    return sha256_jcs({"contribution_kind": contribution_kind, "body": body})
