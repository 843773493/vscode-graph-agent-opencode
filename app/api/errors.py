"""API 适配层的统一异常翻译。

业务服务用 ``NotFoundError``（Session 详情）或目录解析器的 ``KeyError``
（``会话目录节点不存在``）表达「按 ID 查不到目标」。两者都是客户端输入错误，
必须在适配层统一落成 404；``NotFoundError`` 继承 ``HTTPException`` 但基类默认
``status_code=500``，漏接就会把「找不到」报成服务端故障。

detail 沿用仓库既有 404 契约：``NotFoundError`` 取 ``str(error.detail)``，
``KeyError`` 取 ``str(error)``，与 get_session / breadcrumb 等既有入口一致。
"""

from __future__ import annotations

from fastapi import HTTPException

from app.core.exceptions import NotFoundError

__all__ = ["not_found_http_error"]


def not_found_http_error(error: NotFoundError | KeyError) -> HTTPException:
    detail = str(error.detail) if isinstance(error, NotFoundError) else str(error)
    return HTTPException(status_code=404, detail=detail)
