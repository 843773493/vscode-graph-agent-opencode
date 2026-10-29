"""失败的公开事实与原始诊断共用出口。"""

from __future__ import annotations

import logging
import sqlite3
from traceback import format_exception
from typing import TypeVar

logger = logging.getLogger(__name__)
_PROVIDER_FAILURE_ATTRIBUTE = "_boxteam_provider_failure"
_ProviderError = TypeVar("_ProviderError", bound=Exception)


def mark_provider_failure(error: _ProviderError) -> _ProviderError:
    """在不包装异常的前提下记录它来自上游 Provider 边界。"""
    setattr(error, _PROVIDER_FAILURE_ATTRIBUTE, True)
    return error


def is_provider_failure(error: BaseException) -> bool:
    """判断异常是否由 Provider 请求或其原始流迭代直接产生。"""
    return getattr(error, _PROVIDER_FAILURE_ATTRIBUTE, False) is True


def execution_failure_message(error: BaseException) -> str:
    """执行记录保留已知业务说明，原生 I/O 与上游异常不复制诊断正文。"""
    if is_provider_failure(error):
        return failure_detail(error, stage="execution-provider", label="模型执行失败")
    # 无原生字段的 TimeoutError 保留既有业务期限说明，其它 I/O 不发布异常原文。
    if isinstance(error, OSError) and (
        not isinstance(error, TimeoutError)
        or error.errno is not None
        or error.filename is not None
        or error.filename2 is not None
    ):
        return f"执行系统 I/O 失败: error={type(error).__name__}, errno={error.errno}"
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
    try:
        logger.error("%s；完整诊断见异常链", detail, exc_info=error)
    except Exception as sink_error:  # noqa: BLE001 — 不递归写入失败sink，原异常继续携带两份原因。
        error.add_note("诊断日志输出失败：\n" + "".join(format_exception(sink_error)))
        detail += f", diagnostic_sink_error={type(sink_error).__name__}"
    return detail
