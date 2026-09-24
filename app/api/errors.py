"""API 适配层的统一异常翻译。

业务服务用 ``NotFoundError``（Session 详情）或目录解析器的 ``KeyError``
（``会话目录节点不存在``）表达「按 ID 查不到目标」。两者都是客户端输入错误，
必须在适配层统一落成 404；``NotFoundError`` 继承 ``HTTPException`` 但基类默认
``status_code=500``，漏接就会把「找不到」报成服务端故障。

detail 只取面向调用方的可读文本：``NotFoundError`` 的 ``detail`` 是
``{"code", "message", "details"}`` 字典，``KeyError`` 的 ``str()`` 会补上
一对引号；直接 ``str()`` 会把内部字典 或 带引号字面量泄漏成对外契约，
这里统一只抽其中的消息本体。
"""

from __future__ import annotations

from fastapi import HTTPException

from app.core.exceptions import NotFoundError

__all__ = ["not_found_http_error", "unimplemented_http_error"]


def not_found_http_error(error: NotFoundError | KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=_not_found_message(error))


def _not_found_message(error: NotFoundError | KeyError) -> str:
    """抽取稳定的「找不到」消息，不泄漏 Python repr。"""
    if isinstance(error, NotFoundError):
        detail = error.detail
        if isinstance(detail, dict):
            message = detail.get("details") or detail.get("message")
            if message:
                return str(message)
        return str(detail)
    # KeyError 的 str() 会给原始消息补一对引号，取原始参数即可。
    if error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def unimplemented_http_error(error: RuntimeError) -> HTTPException:
    """把「服务端尚未实现的能力」落成 501，而不是无上下文的 500。"""
    return HTTPException(status_code=501, detail=str(error))
