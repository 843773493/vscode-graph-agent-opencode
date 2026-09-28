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

from app.abstractions.state_conflict import ClientStateConflictError
from app.core.exceptions import BaseAPIException, NotFoundError

__all__ = [
    "client_error_message",
    "forbidden_http_error",
    "not_found_http_error",
    "state_conflict_error",
    "state_conflict_http_error",
    "unimplemented_http_error",
]


def not_found_http_error(error: NotFoundError | KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=client_error_message(error))


def client_error_message(error: Exception) -> str:
    """抽取稳定的对外错误文本，不泄漏 Python repr。

    ``BaseAPIException`` 家族（``NotFoundError``/``ForbiddenError``）的
    ``detail`` 是 ``{"code", "message", "details"}`` 字典，只取其中的消息本体
    （唯一实现是 ``BaseAPIException.readable_message``，此处直接复用）；
    直接 ``str(error)`` 会返回 ``"403: {...}"`` 这种带状态码前缀的字典 repr。
    ``KeyError`` 取原始参数，因为 ``str()`` 会给消息补一对引号。
    其余异常类型仍是 ``str(error)``。
    """
    if isinstance(error, BaseAPIException):
        return error.readable_message()
    if isinstance(error, KeyError) and error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def unimplemented_http_error(error: RuntimeError) -> HTTPException:
    """把「服务端尚未实现的能力」落成 501，而不是无上下文的 500。"""
    return HTTPException(status_code=501, detail=str(error))


def state_conflict_error(error: ClientStateConflictError) -> HTTPException:
    """把「客户端可触发的状态冲突」按**类型**落成 409（唯一实现）。

    参数类型即契约：只接受 :class:`ClientStateConflictError`（及其子类）。服务端
    完整性故障是裸 ``RuntimeError``，不满足该注解，路由应当**不捕获**它，由
    ``TraceMiddleware`` 统一落 5xx 并保留完整服务端日志——而不是伪装成 409。

    这是本目录所有「状态冲突 → 409」入口的唯一实现：状态码与文本抽取都收敛在这里，
    各路由只负责把已类型化的异常交给它，不再逐路由手写
    ``HTTPException(status_code=409, detail=...)``，也不做任何消息字符串匹配。
    """
    return _state_conflict_http_exception(error)


def _state_conflict_http_exception(error: Exception) -> HTTPException:
    """状态冲突响应的唯一构造实现（两个公开入口共用，零重复）。"""
    return HTTPException(status_code=409, detail=client_error_message(error))


def state_conflict_http_error(error: Exception) -> HTTPException:
    """[过渡期] 尚未类型化的垂直链路按裸 ``RuntimeError`` 契约落 409。

    存在原因：仍有服务端未把「客户端可触发的状态冲突」换成
    :class:`ClientStateConflictError`。迁移目标是把这些 raiser 换成该类型、路由改调
    :func:`state_conflict_error`（按类型判定）并删除本函数；在那之前保留本函数以维持
    既有 409 契约（零回归）。

    它与 :func:`state_conflict_error` 共用 ``_state_conflict_http_exception`` 这一份
    状态码与文本抽取实现，不是双轨；区别只在声明的参数契约（宽 vs 精确）。
    """
    return _state_conflict_http_exception(error)


def forbidden_http_error(error: BaseAPIException) -> HTTPException:
    """把路径越界等 ``ForbiddenError`` 统一落成 403，并只下发消息本体。

    ``ForbiddenError`` 继承 ``HTTPException`` 但基类 ``status_code=500``，且
    ``str()`` 返回 ``"500: {'code': 403000, ...}"``。既不能让它冒泡成 500，
    也不能把内部字典 repr 当 detail 下发，故统一在此显式落 403 + 抽取文本。
    """
    return HTTPException(status_code=403, detail=client_error_message(error))
