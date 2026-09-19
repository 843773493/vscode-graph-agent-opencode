"""把已封存 assembly 的原生投影短暂交给 Provider adapter。

这里是无状态 dispatch bridge：它不捕获 middleware 差异、不重建上下文、
不保存 replay 队列。Saver 在进入 bridge 前已经完成 selection、hash 和
assembly 校验；ContextVar 只承载当前 handler 生命周期内的只读引用。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_SEALED_NATIVE_PROJECTION: ContextVar[dict[str, object] | None] = ContextVar(
    "sealed_native_request_projection",
    default=None,
)


@contextmanager
def sealed_native_projection_scope(
    projection: Mapping[str, object] | None,
) -> Iterator[None]:
    """仅在本次 Provider handler 生命周期内传递 Saver 的 sealed projection。"""
    if projection is not None:
        required = ("session_id", "assembly_id", "plan_hash", "selection", "request")
        if any(key not in projection for key in required):
            raise TypeError("sealed assembly projection 缺少必需 manifest 字段")
        if not isinstance(projection["session_id"], str):
            raise TypeError("sealed assembly projection.session_id 必须是字符串")
        if not isinstance(projection["assembly_id"], str):
            raise TypeError("sealed assembly projection.assembly_id 必须是字符串")
        if not isinstance(projection["plan_hash"], str):
            raise TypeError("sealed assembly projection.plan_hash 必须是字符串")
        if not isinstance(projection["selection"], list):
            raise TypeError("sealed assembly projection.selection 必须是列表")
        if not isinstance(projection["request"], dict):
            raise TypeError("sealed assembly projection.request 必须是对象")
    token = _SEALED_NATIVE_PROJECTION.set(
        copy.deepcopy(dict(projection)) if projection is not None else None
    )
    try:
        yield
    finally:
        _SEALED_NATIVE_PROJECTION.reset(token)


def read_sealed_native_projection() -> dict[str, object] | None:
    """返回当前调用的独立 projection 副本，禁止 Provider 原地修改 manifest。"""
    value = _SEALED_NATIVE_PROJECTION.get()
    return copy.deepcopy(value) if value is not None else None


__all__ = ["read_sealed_native_projection", "sealed_native_projection_scope"]
