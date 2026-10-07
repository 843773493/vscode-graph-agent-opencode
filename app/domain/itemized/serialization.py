"""Provider-neutral JSON 投影与稳定排序；内部 hash 始终覆盖精确正文。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.validation import _non_empty_string

# 只剔除请求参数层的认证、传输、attempt 和时间载体。正文与 schema 的同名字段
# 属于实际请求语义，必须逐字节参与规范化 hash。
_VOLATILE_REQUEST_KEYS = frozenset({
    "authorization", "auth", "headers", "provider_request_id", "request_id",
    "response_id", "retry", "attempt", "created_at", "timestamp", "trace_id",
    "span_id", "transport",
})


def _hash_safe_value(value: object) -> object:
    """构造可规范化 JSON 副本，不以字段名改写正文。"""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ItemSchemaError("hash preimage object key 必须是字符串")
        return {key: _hash_safe_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hash_safe_value(child) for child in value]
    return value


def project_declared_tools(value: object) -> object:
    """保持工具声明的完整语义。"""
    return _hash_safe_value(value)


def normalize_wire_request(value: object) -> object:
    """实际规范化 wire；仅请求参数层的易变载体不参与 hash。"""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ItemSchemaError("wire request object key 必须是字符串")
        return {
            key: _hash_safe_value(child) for key, child in value.items()
            if key.casefold() not in _VOLATILE_REQUEST_KEYS
        }
    return _hash_safe_value(value)


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
    "normalize_wire_request",
    "ordered_selection",
    "project_declared_tools",
]
