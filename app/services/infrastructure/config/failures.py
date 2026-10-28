"""配置失败的公开事实与原始诊断共用出口。"""

from __future__ import annotations

import logging
import sqlite3
from traceback import format_exception

logger = logging.getLogger(__name__)


def config_failure_detail(error: BaseException, *, stage: str, label: str) -> str:
    """不把异常正文复制到状态或事件，日志故障也不能阻止原配置结算。"""
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
