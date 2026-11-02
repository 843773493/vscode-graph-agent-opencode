"""失败的公开事实与原始诊断共用出口。"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from traceback import format_exception
from typing import TypeVar

from pydantic import ValidationError

from app.domain.itemized.errors import ItemSchemaError

logger = logging.getLogger(__name__)
_PROVIDER_FAILURE_ATTRIBUTE = "_boxteam_provider_failure"
_PUBLIC_FAILURE_ATTRIBUTE = "_boxteam_public_failure"
_ProviderError = TypeVar("_ProviderError", bound=Exception)
_PublicError = TypeVar("_PublicError", bound=BaseException)


def mark_public_failure(error: _PublicError, message: str | None = None) -> _PublicError:
    """由错误来源登记已核定的公开说明，保持原异常实例、类别和 cause。"""
    setattr(error, _PUBLIC_FAILURE_ATTRIBUTE, message if message is not None else f"{type(error).__name__}: {error}")
    return error


def public_failure_message(error: BaseException) -> str | None:
    """只读取显式登记的说明，不根据异常类别或正文猜测是否可公开。"""
    return getattr(error, _PUBLIC_FAILURE_ATTRIBUTE, None)


def mark_provider_failure(error: _ProviderError) -> _ProviderError:
    """在不包装异常的前提下记录它来自上游 Provider 边界。"""
    setattr(error, _PROVIDER_FAILURE_ATTRIBUTE, True)
    return error


def is_provider_failure(error: BaseException) -> bool:
    """判断异常是否由 Provider 请求或其原始流迭代直接产生。"""
    return getattr(error, _PROVIDER_FAILURE_ATTRIBUTE, False) is True


def execution_failure_message(error: BaseException) -> str:
    """执行记录保留已知文件原因；上游、内部校验和数据库正文仍按来源保护。"""
    if is_provider_failure(error):
        return failure_detail(error, stage="execution-provider", label="模型执行失败")
    if isinstance(error, (ValidationError, ItemSchemaError)):
        # 内部结构拒绝可能携带动态键、字段值或 input_value；原错误交原执行出口记录。
        return f"执行数据校验失败: error={type(error).__name__}"
    if isinstance(error, sqlite3.Error):
        return f"执行数据库失败: error={type(error).__name__}, sqlite_errorcode={getattr(error, 'sqlite_errorcode', None)}"
    # 有 filename 的系统错误明确属于文件操作；不从任意异常正文猜测文件或目标。
    if isinstance(error, OSError) and (
        not isinstance(error, TimeoutError)
        or error.errno is not None
        or error.filename is not None
        or error.filename2 is not None
    ):
        detail = f"执行系统 I/O 失败: error={type(error).__name__}, errno={error.errno}"
        if error.errno is not None and error.filename is not None:
            detail += f", reason={error.strerror}, filename={error.filename!r}"
            if error.filename2 is not None:
                detail += f", filename2={error.filename2!r}"
        return detail
    return str(error)


def failure_detail(error: BaseException, *, stage: str, label: str) -> str:
    """不把异常正文复制到状态或事件，日志故障也不能阻止原失败结算。"""
    detail = f"{label}: stage={stage}, error={type(error).__name__}"
    if isinstance(error, OSError):
        detail += f", errno={error.errno}"
        if (winerror := getattr(error, "winerror", None)) is not None:
            detail += f", winerror={winerror}"
    elif isinstance(error, sqlite3.Error):
        detail += f", sqlite_errorcode={getattr(error, 'sqlite_errorcode', None)}"
    for coordinate in ("lineno", "colno", "pos"):
        if isinstance(value := getattr(error, coordinate, None), int):
            detail += f", {coordinate}={value}"
    return report_failure(error, detail=detail)


def report_failure(
    error: BaseException, *, detail: str,
    diagnostic_logger: logging.Logger = logger,
    extra: Mapping[str, object] | None = None,
) -> str:
    """记录原始诊断，保留调用方已分离的公开事实及既有格式。"""
    try:
        diagnostic_logger.error("%s；完整诊断见异常链", detail, exc_info=error, extra=extra)
    except Exception as sink_error:  # noqa: BLE001 — 不递归写入失败sink，原异常继续携带两份原因。
        error.add_note("诊断日志输出失败：\n" + "".join(format_exception(sink_error)))
        detail += f", diagnostic_sink_error={type(sink_error).__name__}"
    return detail
