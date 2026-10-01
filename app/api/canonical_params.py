"""API 层 canonical ID 形态（OpenSpec 2.1）。

路径/查询/请求体中的 session_id 在进入业务逻辑与落盘之前先经
app.core.session_catalog_store 的唯一 canonical 验证器校验；非法形态由
pydantic/FastAPI 统一转成 422，不做清洗、截断或旧 ID 别名。
校验器只有一套，本模块只提供 Annotated 复用类型。

thread_id 不走本模块：产品入口的 thread 身份允许字面量 `main`（main
thread），与 `thr_` 前缀的 canonical 形态不同，其形态校验由 thread 目录
解析器在业务层统一执行（app/core/session_catalog_resolver）。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator

from app.core.session_catalog_store import validate_session_id

__all__ = ["CanonicalSessionId"]


def _validate_canonical_session_id(value: object) -> str:
    """唯一验证器抛出的 TypeError 统一转成 ValueError（pydantic → 422）。"""
    try:
        validate_session_id(value)
    except TypeError as error:
        raise ValueError(str(error)) from error
    return value


CanonicalSessionId = Annotated[
    str,
    BeforeValidator(_validate_canonical_session_id),
]
