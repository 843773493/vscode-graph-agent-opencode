"""API 适配层的统一异常翻译。

业务服务用 ``NotFoundError``（Session 详情）或目录解析器的 ``KeyError``
（``会话目录节点不存在``）表达「按 ID 查不到目标」。两者都是客户端输入错误，
必须在适配层统一落成 404；``NotFoundError`` 继承 ``HTTPException`` 但基类默认
``status_code=500``，漏接就会把「找不到」报成服务端故障。

``ForbiddenError``（``safe_join`` 的路径越界）同样继承 ``HTTPException`` 且基类
默认 ``status_code=500``，漏接会把客户端路径错误报成 500；即使已捕获，
``str(error)`` 仍返回 ``"500: {'code': 403000, ...}"`` 这种带状态码前缀的内部
字典形态。这里统一抽取可读消息本体并显式落 403。

detail 只取面向调用方的可读文本：``NotFoundError`` 的 ``detail`` 是
``{"code", "message", "details"}`` 字典，``KeyError`` 的 ``str()`` 会补上
一对引号；直接 ``str()`` 会把内部字典 或 带引号字面量泄漏成对外契约，
这里统一只抽其中的消息本体。

同一套抽取也用于把 ``KeyError`` 落成 400 的入口（非法 cursor、未知 operation
ID 等），避免同一份消息在不同状态码下出现两种文本形态。
"""

from __future__ import annotations

from fastapi import HTTPException

from app.core.exceptions import BaseAPIException, NotFoundError

__all__ = [
    "client_error_message",
    "forbidden_http_error",
    "not_found_http_error",
    "state_conflict_http_error",
    "unimplemented_http_error",
]


def not_found_http_error(error: NotFoundError | KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=client_error_message(error))


def client_error_message(error: Exception) -> str:
    """抽取稳定的对外错误文本，不泄漏 Python repr。

    ``BaseAPIException`` 家族（``NotFoundError``/``ForbiddenError``）的
    ``detail`` 是 ``{"code", "message", "details"}`` 字典，只取其中的消息本体；
    直接 ``str(error)`` 会返回 ``"500: {...}"`` 这种带状态码前缀的字典 repr。
    ``KeyError`` 取原始参数，因为 ``str()`` 会给消息补一对引号。
    其余异常类型仍是 ``str(error)``。
    """
    if isinstance(error, BaseAPIException):
        detail = error.detail
        if isinstance(detail, dict):
            message = detail.get("details") or detail.get("message")
            if message:
                return str(message)
        return str(detail)
    if isinstance(error, KeyError) and error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def unimplemented_http_error(error: RuntimeError) -> HTTPException:
    """把「服务端尚未实现的能力」落成 501，而不是无上下文的 500。"""
    return HTTPException(status_code=501, detail=str(error))


def state_conflict_http_error(error: Exception) -> HTTPException:
    """把「客户端可触发的服务端状态冲突」统一落成 409。

    业务服务用 ``RuntimeError``/``ValueError``/``TypeError`` 表达「当前状态
    不允许该动作」「与在途操作冲突」等可恢复状态冲突。这类异常若漏接，会被
    ``TraceMiddleware`` 统一转成无上下文的 500，并把 ``RuntimeError:`` 之类
    的内部类名前缀写进响应体。适配层必须显式落成 409，让客户端据此重试或修正
    时序，而不是收到 5xx。

    这是本目录所有「状态冲突 → 409」入口的唯一实现：sessions、session_navigation、
    messages、tools、node_debug、workspace、config、agents 与 runtime 共用它，
    不再逐路由手写同一份 ``HTTPException(status_code=409, detail=...)``。
    """
    return HTTPException(status_code=409, detail=client_error_message(error))


def forbidden_http_error(error: BaseAPIException) -> HTTPException:
    """把路径越界等 ``ForbiddenError`` 统一落成 403，并只下发消息本体。

    ``ForbiddenError`` 继承 ``HTTPException`` 但基类 ``status_code=500``，且
    ``str()`` 返回 ``"500: {'code': 403000, ...}"``。既不能让它冒泡成 500，
    也不能把内部字典 repr 当 detail 下发，故统一在此显式落 403 + 抽取文本。
    """
    return HTTPException(status_code=403, detail=client_error_message(error))
