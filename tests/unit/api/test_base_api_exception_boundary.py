"""冻结 ``BaseAPIException`` 家族「无路由级 except 也不泄漏成 500」的结构性收口。

``ForbiddenError``/``NotFoundError`` 继承 ``HTTPException``，基类过去硬编码
``status_code=500`` 且 ``detail`` 是内部字典。只要某条路由漏接，异常冒泡到
全局 ``HTTPException`` 处理器就会下发 500 + 内部字典 repr。

本文件锁住两层保障：
- ``BaseAPIException`` 自身按 ``code // 1000`` 派生语义状态码，并提供
  ``readable_message()`` 抽取纯文本 detail；
- ``app.main`` 注册了比 ``HTTPException`` 更具体的 ``BaseAPIException``
  处理器，Starlette 按异常具体程度派发，冒泡即得 4xx + 纯文本，且不影响
  普通 ``HTTPException``。

探测路由在测试内临时挂载并在 finally 中移除，避免污染真实 app 的路由表。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.api.errors import client_error_message, forbidden_http_error
from app.core.exceptions import BaseAPIException, ForbiddenError, NotFoundError
from app.main import app, workspace_base_api_exception_handler


def test_base_api_exception_status_is_derived_from_code() -> None:
    assert ForbiddenError("x").status_code == 403
    assert NotFoundError("x").status_code == 404
    assert BaseAPIException("x").status_code == 500


def test_readable_message_never_returns_dict_repr() -> None:
    assert ForbiddenError("Path traversal detected").readable_message() == (
        "Path traversal detected"
    )
    # 无 details 时回退到 message，同样不是字典 repr。
    assert ForbiddenError().readable_message() == "forbidden"
    assert "{" not in ForbiddenError().readable_message()


def test_forbidden_http_error_delegates_to_readable_message() -> None:
    error = ForbiddenError("Path traversal detected")
    assert client_error_message(error) == "Path traversal detected"
    assert forbidden_http_error(error).detail == "Path traversal detected"


def test_base_api_exception_handler_is_registered_for_the_subclass() -> None:
    """收口点必须注册在 ``BaseAPIException`` 这一具体类型上。"""
    assert app.exception_handlers[BaseAPIException] is (
        workspace_base_api_exception_handler
    )


@pytest.mark.parametrize(
    ("path", "raised", "expected_status", "expected_detail"),
    [
        (
            "/api/v1/__probe_forbidden",
            lambda: ForbiddenError("Path traversal detected"),
            403,
            "Path traversal detected",
        ),
        (
            "/api/v1/__probe_missing",
            lambda: NotFoundError("Session ses_x not found"),
            404,
            "Session ses_x not found",
        ),
    ],
)
def test_bubbling_base_api_exception_is_never_a_500(
    path: str,
    raised: Callable[[], BaseAPIException],
    expected_status: int,
    expected_detail: str,
) -> None:
    """路由刻意不写任何 except，异常必须由全局收口成 4xx + 纯文本。"""

    async def handler(request: Request):
        raise raised()

    app.add_api_route(path, handler, methods=["GET"])
    route = app.router.routes[-1]
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            path,
            headers={"X-Request-ID": "req_base_api_boundary"},
        )
    finally:
        app.router.routes.remove(route)

    assert response.status_code == expected_status
    assert response.headers["X-Request-ID"] == "req_base_api_boundary"
    assert response.json() == {
        "detail": expected_detail,
        "request_id": "req_base_api_boundary",
    }


def test_plain_http_exception_is_unaffected_by_base_handler() -> None:
    """非 ``BaseAPIException`` 的普通 ``HTTPException`` 仍走通用处理器。"""
    path = "/api/v1/__probe_plain_http"

    async def handler(request: Request):
        raise HTTPException(status_code=418, detail="teapot")

    app.add_api_route(path, handler, methods=["GET"])
    route = app.router.routes[-1]
    try:
        response = TestClient(app, raise_server_exceptions=False).get(path)
    finally:
        app.router.routes.remove(route)

    assert response.status_code == 418
    assert response.json()["detail"] == "teapot"
