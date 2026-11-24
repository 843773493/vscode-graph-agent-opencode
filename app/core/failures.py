"""失败的公开事实与原始诊断共用出口。"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from copy import deepcopy
from typing import TypedDict, TypeVar

from boxteam_plugin_sdk.failures import (
    DIAGNOSTIC_BYTES,
    PUBLIC_ERROR_BYTES,
    PublicError,
    bounded_failure_text,
    failure_message,
    format_failure_diagnostic,
)
from boxteam_plugin_sdk.failures import (
    public_error as build_public_error,
)

logger = logging.getLogger(__name__)
_PROVIDER_FAILURE_ATTRIBUTE = "_boxteam_provider_failure"
_PUBLIC_FAILURE_ATTRIBUTE = "_boxteam_public_failure"
_HTTP_FAILURE_ATTRIBUTE = "_boxteam_http_failure"
HTTP_FAILURE_HEADER = "X-BoxTeam-Error-Context"
_ProviderError = TypeVar("_ProviderError", bound=Exception)
_PublicError = TypeVar("_PublicError", bound=BaseException)


class HttpFailureContext(TypedDict, total=False):
    source: dict[str, str]
    target: dict[str, str]
    stage: str
    error_type: str
    status_code: int


def mark_http_failure(error: BaseException, context: HttpFailureContext) -> None:
    """传输边界只登记已知事实，保留原异常身份供既有重试与收尾消费。"""
    setattr(error, _HTTP_FAILURE_ATTRIBUTE, context)


def http_failure_context(error: BaseException | None) -> HttpFailureContext:
    """沿显式因果链寻找传输来源，业务包装不能丢失原失败位置。"""
    while error is not None:
        context: HttpFailureContext | None = getattr(error, _HTTP_FAILURE_ATTRIBUTE, None)
        if context is not None:
            return context.copy()
        error = error.__cause__
    return {}


def mark_public_failure(error: _PublicError, message: str | None = None) -> _PublicError:
    """由错误来源登记已核定的公开说明，保持原异常实例、类别和 cause。"""
    text = message if message is not None else f"{type(error).__name__}: {failure_message(error)}"
    setattr(error, _PUBLIC_FAILURE_ATTRIBUTE, bounded_failure_text(text))
    return error


def public_failure_message(error: BaseException) -> str | None:
    """只读取显式登记的说明，不根据异常类别或正文猜测是否可公开。"""
    return getattr(error, _PUBLIC_FAILURE_ATTRIBUTE, None)


def mark_provider_failure(
    error: _ProviderError, *, secrets: tuple[str | None, ...] = (),
    diagnostic_logger: logging.Logger | None = None,
) -> _ProviderError:
    """原来源消费当次凭据，只留下安全事实；多层捕获不重复提取原异常。"""
    if is_provider_failure(error):
        return error
    known_secrets = tuple(secret for secret in secrets if secret)
    # 为消费者覆盖 code/stage 预留 JSON 转义预算，保留同一安全原因树。
    fact = build_public_error(error, secrets=known_secrets, max_bytes=PUBLIC_ERROR_BYTES - 4096)
    if diagnostic_logger is not None:
        report_failure(error, detail=f"模型上游请求失败: error={type(error).__name__}",
            diagnostic_logger=diagnostic_logger, secrets=known_secrets)
    mark_public_failure(error, fact["message"])
    setattr(error, _PROVIDER_FAILURE_ATTRIBUTE, fact)
    return error


def is_provider_failure(error: BaseException) -> bool:
    """判断异常是否由 Provider 请求或其原始流迭代直接产生。"""
    return isinstance(getattr(error, _PROVIDER_FAILURE_ATTRIBUTE, None), dict)


def public_error(
    error: BaseException, *, code: str = "unknown_error", stage: str | None = None,
) -> PublicError:
    """Provider 只消费原来源已脱敏的原因树，其余异常复用 SDK 出口。"""
    fact: PublicError | None = getattr(error, _PROVIDER_FAILURE_ATTRIBUTE, None)
    if fact is None:
        return build_public_error(error, code=code, stage=stage)
    result = deepcopy(fact)
    result["code"] = bounded_failure_text(code, max_bytes=256)
    if stage:
        result["details"]["stage"] = bounded_failure_text(stage, max_bytes=256)
    return result


def execution_failure_message(error: BaseException) -> str:
    """保留原执行分类，以及经过统一脱敏和预算处理的真实原因。"""
    if is_provider_failure(error):
        return failure_detail(error, stage="execution-provider", label="模型执行失败")
    if isinstance(error, sqlite3.Error):
        return bounded_failure_text(
            f"执行数据库失败: error={type(error).__name__}, sqlite_errorcode={getattr(error, 'sqlite_errorcode', None)}, "
            f"reason={failure_message(error)}"
        )
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
        return bounded_failure_text(detail)
    return failure_message(error)


def failure_detail(error: BaseException, *, stage: str, label: str, secrets: tuple[str, ...] = ()) -> str:
    """原阶段和分类附带安全原因，日志故障也不能阻止原失败结算。"""
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
    detail += ", reason=" + (public_failure_message(error) or failure_message(error, secrets=secrets))
    return report_failure(error, detail=detail, secrets=secrets)


def report_failure(
    error: BaseException, *, detail: str,
    diagnostic_logger: logging.Logger = logger,
    extra: Mapping[str, object] | None = None,
    secrets: tuple[str, ...] = (),
) -> str:
    """沿原日志出口记录有界安全诊断，不重复输出原始堆栈。"""
    detail = bounded_failure_text(detail, secrets=secrets)
    diagnostic = (
        "" if is_provider_failure(error) else format_failure_diagnostic(
            error, secrets=secrets, max_bytes=DIAGNOSTIC_BYTES - len(detail.encode("utf-8")) - 1,
        )
    )
    try:
        diagnostic_logger.error("%s\n%s", detail, diagnostic, extra=extra)
    except Exception as sink_error:  # noqa: BLE001 — 不递归写入失败sink，原异常继续携带两份原因。
        # Provider 凭据已离开源边界，不能经 sink 的隐式 context 重读原异常。
        sink_diagnostic = (
            f"error={type(sink_error).__name__}, errno={getattr(sink_error, 'errno', None)}"
            if is_provider_failure(error) else format_failure_diagnostic(sink_error, secrets=secrets)
        )
        error.add_note("诊断日志输出失败：\n" + sink_diagnostic)
        detail += f", diagnostic_sink_error={type(sink_error).__name__}"
    return bounded_failure_text(detail)
