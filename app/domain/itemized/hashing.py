"""RFC 8785 JCS v1 hash primitives。"""

from __future__ import annotations

import hashlib
import math
import re
import sys
from collections.abc import Mapping
from operator import itemgetter

import orjson
import rfc8785

from app.domain.itemized.errors import ItemSchemaError

_SHA256_JCS_PATTERN = re.compile(r"^sha256:jcs:v1:[0-9a-f]{64}$")
_UNICODE_SURROGATE_PATTERN = re.compile(r"[\ud800-\udfff]")
_JSON_SCALAR_TYPES = frozenset({str, int, float, bool, type(None)})


def copy_json_value(value: object) -> object:
    """隔离 JSON 容器的引用，保留 list/tuple 的既有序列化形状。"""
    # 普通 JSON 叶值不可变，无需逐个执行 Mapping ABC 检测。
    # 只匹配精确类型，子类仍按原 Mapping/容器语义复制，不改变隔离边界。
    if type(value) in _JSON_SCALAR_TYPES:
        return value
    if isinstance(value, Mapping):
        return {key: copy_json_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [copy_json_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(copy_json_value(child) for child in value)
    return value


def _ensure_json_value(value: object, path: str = "value") -> None:
    _validate_json_value(value, path)


def _validate_json_value(value: object, path: str) -> None:
    """完整校验 JSON，报告原始字段路径；不在这里转换数字或容器。"""
    if isinstance(value, str):
        if _UNICODE_SURROGATE_PATTERN.search(value) is not None:
            raise ItemSchemaError(f"{path!r} 包含非法 Unicode surrogate")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ItemSchemaError(f"{path} 包含非有限浮点数")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ItemSchemaError(f"{path} object key 必须是字符串")
            _validate_json_value(key, f"{path} object key")
            _validate_json_value(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{path}[{index}]")
        return
    if value is None or isinstance(value, (int, bool)):
        return
    if isinstance(value, float):
        return
    raise ItemSchemaError(f"{path} 不是 JSON value: {type(value).__name__}")


def _jcs_input(value: object) -> object:
    """按 UTF-16 排好对象键；数字仍由 RFC 8785 编码，字节输出交给原生编码器。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        value = int(value)
        if abs(value) <= 2**53 - 1:
            return value
        if abs(value) > int(sys.float_info.max):
            raise ItemSchemaError("整数超出 IEEE-754 有限数字范围")
        number = float(value)
        if int(number) != value and rfc8785.dumps(number).decode("utf-8") != str(value):
            raise ItemSchemaError("整数不能无损映射到 JCS/IEEE-754，须编码为字符串")
        return orjson.Fragment(rfc8785.dumps(number))
    if isinstance(value, float):
        return orjson.Fragment(rfc8785.dumps(float(value)))
    if isinstance(value, Mapping):
        items = list(value.items())
        # ASCII 键的字符串序与 UTF-16 序相同，不为排序逐键编码。
        # 字符串子类保留原 encode 语义；仅选择比较键，不改变 JCS 投影。
        items.sort(key=itemgetter(0) if all(
            type(key) is str and key.isascii() for key, _ in items
        ) else lambda item: item[0].encode("utf-16be"))
        return {str.__str__(key): _jcs_input(child) for key, child in items}
    if isinstance(value, (list, tuple)):
        return [_jcs_input(child) for child in value]
    raise TypeError(f"不是 JSON value: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """返回 RFC 8785 JCS 的无空白 UTF-8 字节。"""
    try:
        return orjson.dumps(_jcs_input(value))
    except (rfc8785.CanonicalizationError, UnicodeError, TypeError, AttributeError, ItemSchemaError) as error:
        # 只在失败时完整验证原始输入，保留结构错误路径和数字范围错误的既有先后。
        _validate_json_value(value, "value")
        if isinstance(error, ItemSchemaError):
            raise
        raise ItemSchemaError(f"RFC 8785 JCS 无法编码 value: {error}") from error


def canonical_json_bytes_and_hash(value: object) -> tuple[bytes, str]:
    """同次规范化的不可变字节与摘要；不持有输入引用或跨调用缓存。"""
    encoded = canonical_json_bytes(value)
    return encoded, "sha256:jcs:v1:" + hashlib.sha256(encoded).hexdigest()


def sha256_jcs(value: object) -> str:
    return canonical_json_bytes_and_hash(value)[1]


def validate_hash_token(value: object, field_name: str) -> str:
    """校验内部唯一 content_hash 的跨进程 token 语法。"""
    if not isinstance(value, str) or _SHA256_JCS_PATTERN.fullmatch(value) is None:
        raise ItemSchemaError(f"{field_name} 必须符合 sha256:jcs:v1:<64位小写hex>")
    return value


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
    return _contribution_content_bytes_and_hash(contribution_kind, body)[1]


def contribution_content_hash_and_length(
    contribution_kind: str, body: object,
) -> tuple[str, int]:
    """同次 JCS 编码给出 contribution 摘要与正文的结构化长度。"""
    encoded, digest = _contribution_content_bytes_and_hash(contribution_kind, body)
    # JCS 固定按 body、contribution_kind 排列两个键；扣除信封即可取得正文长度，
    # 不为长度再编码正文。kind 的转义仍交给同一 JCS 编码器处理。
    body_length = len(encoded) - len(canonical_json_bytes(contribution_kind)) - len(
        b'{"body":,"contribution_kind":}'
    )
    return digest, body_length


def _contribution_content_bytes_and_hash(
    contribution_kind: str, body: object,
) -> tuple[bytes, str]:
    if not isinstance(contribution_kind, str) or not contribution_kind:
        raise ItemSchemaError("contribution_kind 必须是非空字符串")
    return canonical_json_bytes_and_hash({
        "contribution_kind": contribution_kind, "body": body,
    })
