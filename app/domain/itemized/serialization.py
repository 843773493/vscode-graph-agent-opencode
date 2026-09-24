"""v2 provider-neutral serialization and stable ordering helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.redaction import validate_hash_redaction
from app.domain.itemized.refs import selection_ref_identity

# 凭据字段必须先由 producer 显式脱敏；token 数量、工具 schema 等不是凭据。
_CREDENTIAL_KEYS = frozenset({
    "token", "access_token", "refresh_token", "api_key", "secret", "client_secret",
    "password", "credential", "credentials",
})


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


def _hash_safe_value(value: object) -> object:
    """消费 producer 预先生成的完整 marker，不创建会造成碰撞的统一占位符。"""
    if isinstance(value, Mapping):
        if "redaction_class" in value:
            return validate_hash_redaction(value)
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ItemSchemaError("hash preimage object key 必须是字符串")
            key_text = key
            if key_text.lower() in _CREDENTIAL_KEYS:
                if not isinstance(child, Mapping):
                    raise ItemSchemaError("hash-redaction-required: 凭据必须使用 class/length/session digest")
                result[key_text] = validate_hash_redaction(child)
                continue
            result[key_text] = _hash_safe_value(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_hash_safe_value(child) for child in value]
    return value


_VOLATILE_REQUEST_KEYS = frozenset(
    {
        "authorization",
        "auth",
        "headers",
        "provider_request_id",
        "request_id",
        "response_id",
        "retry",
        "attempt",
        "created_at",
        "timestamp",
        "trace_id",
        "span_id",
        "transport",
    }
)


def normalize_wire_request(value: object) -> object:
    """保留 request 语义，剔除认证、传输和 attempt volatile 字段。"""
    return _hash_safe_value(_without_volatile_request_fields(value))


def _without_volatile_request_fields(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ItemSchemaError("wire request object key 必须是字符串")
            key_text = key
            if key_text.lower() in _VOLATILE_REQUEST_KEYS:
                continue
            result[key_text] = _without_volatile_request_fields(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_without_volatile_request_fields(child) for child in value]
    return value


def _contribution_order_key(value: object) -> tuple[int, str]:
    """返回唯一稳定的 request hash contribution 顺序。"""
    ordinal = getattr(value, "contribution_ordinal", None)
    if ordinal is None:
        # source_ordinal 只来自 itemized registry 分配的 typed 字段；自由
        # metadata 的同名键不再参与 request hash 排序。
        raw = getattr(value, "source_ordinal", None)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ItemSchemaError(
                "request hash contribution 缺少稳定 ordinal: "
                f"{getattr(value, 'contribution_id', '<unknown>')}"
            )
        ordinal = raw
    return (
        ordinal,
        _non_empty_string(
            getattr(value, "contribution_id", None),
            "contribution_id",
        ),
    )


def ordered_selection(entries: Sequence[object]) -> tuple[object, ...]:
    values = tuple(entries)
    ordinals = [getattr(value, "plan_ordinal", None) for value in values]
    if any(
        not isinstance(ordinal, int) or isinstance(ordinal, bool)
        for ordinal in ordinals
    ) or ordinals != list(range(len(values))):
        raise ItemSchemaError("selection.plan_ordinal 必须从 0 连续递增")
    # identity 口径统一由 selection_ref_identity 提供：ContextRef 使用
    # thread-qualified (ref_type, ref_id, thread_id)，不得在这里退回二元组。
    identities = [
        selection_ref_identity(getattr(value, "ref", None)) for value in values
    ]
    if len(identities) != len(set(identities)):
        raise ItemSchemaError("selection 不得重复引用同一个 ref")
    return values


__all__ = [
    "_contribution_order_key",
    "_hash_safe_value",
    "_non_empty_string",
    "normalize_wire_request",
    "ordered_selection",
]
