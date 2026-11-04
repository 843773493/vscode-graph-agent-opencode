"""API 与 Pydantic DTO 共用的 canonical ID 校验类型。"""

from __future__ import annotations

from typing import Annotated, cast

from pydantic import AfterValidator, BeforeValidator

from app.core.identifier import (
    validate_gateway_id,
    validate_session_id,
    validate_thread_id,
)
from app.core.workspace_identity import validate_workspace_id

__all__ = ["CanonicalGatewayId", "CanonicalSessionId", "CanonicalThreadId", "CanonicalWorkspaceId"]


CanonicalGatewayId = Annotated[str, AfterValidator(validate_gateway_id)]


def _validate_canonical_workspace_id(value: str) -> str:
    """复用身份 owner，将运行时格式拒绝转换为 DTO 字段错误。"""
    try:
        return validate_workspace_id(value)
    except RuntimeError as error:
        raise ValueError(str(error)) from error


CanonicalWorkspaceId = Annotated[str, AfterValidator(_validate_canonical_workspace_id)]


def _validate_canonical_session_id(value: object) -> str:
    """将 session validator 的类型错误转换为 Pydantic 校验错误。"""
    try:
        validate_session_id(value)
    except TypeError as error:
        raise ValueError(str(error)) from error
    return cast(str, value)


CanonicalSessionId = Annotated[
    str,
    BeforeValidator(_validate_canonical_session_id),
]


def _validate_canonical_thread_id(value: object) -> str:
    """将 thread validator 的类型错误转换为 Pydantic 校验错误。"""
    try:
        validate_thread_id(value)
    except TypeError as error:
        raise ValueError(str(error)) from error
    return cast(str, value)


CanonicalThreadId = Annotated[
    str,
    BeforeValidator(_validate_canonical_thread_id),
]
