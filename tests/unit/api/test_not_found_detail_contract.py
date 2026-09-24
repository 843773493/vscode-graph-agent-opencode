"""冻结「找不到」404 响应体的 detail 形态，禁止泄漏 Python repr。

``NotFoundError.detail`` 是 ``{"code", "message", "details"}`` 字典；
``KeyError`` 的 ``str()`` 会给原始消息补一对引号。两者直接下发给调用方都会
把内部字面量当成契约（信息泄漏 + 契约不稳定），这里锁住只下发消息本体。
"""

from __future__ import annotations

from app.api.errors import not_found_http_error
from app.core.exceptions import NotFoundError


def test_not_found_error_detail_does_not_leak_dict_repr() -> None:
    error = NotFoundError("Session ses_x not found")

    detail = not_found_http_error(error).detail

    assert detail == "Session ses_x not found"
    assert "{" not in str(detail)
    assert "code" not in str(detail)


def test_key_error_detail_does_not_leak_quotes() -> None:
    error = KeyError("会话目录节点不存在: ses_x")

    detail = not_found_http_error(error).detail

    assert detail == "会话目录节点不存在: ses_x"
    assert not str(detail).startswith("'")


def test_not_found_error_falls_back_to_message_when_no_details() -> None:
    error = NotFoundError()

    detail = not_found_http_error(error).detail

    assert isinstance(detail, str)
    assert "{" not in detail
