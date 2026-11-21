"""SDK request_id 的唯一绑定、读取和嵌套恢复。"""

import pytest
from boxteam_plugin_sdk.request_context import (
    get_current_request_id,
    require_current_request_id,
    reset_current_request_id,
    set_current_request_id,
)


def test_require_current_request_id_fails_closed_without_binding() -> None:
    """无请求绑定即 fail-closed 抛错，且消息带消费者名；MUST NOT 回退虚假默认值。"""
    assert get_current_request_id() is None

    with pytest.raises(RuntimeError) as excinfo:
        require_current_request_id("测试消费方")

    message = str(excinfo.value)
    assert "测试消费方" in message
    assert "未绑定" in message


def test_require_current_request_id_returns_bound_authority_verbatim() -> None:
    """与宽容版读同一 ContextVar：绑定后原样返回；复位后回到 fail-closed。"""
    token = set_current_request_id("req_bound_authority")
    try:
        assert get_current_request_id() == "req_bound_authority"
        assert require_current_request_id("测试消费方") == "req_bound_authority"
    finally:
        reset_current_request_id(token)

    assert get_current_request_id() is None
    with pytest.raises(RuntimeError):
        require_current_request_id("测试消费方")


def test_require_current_request_id_rejects_blank_binding() -> None:
    """空串等同无绑定：fail-closed 不把空串当合法权威 ID（与 gateway_id 守卫一致）。"""
    token = set_current_request_id("")
    try:
        with pytest.raises(RuntimeError):
            require_current_request_id("测试消费方")
    finally:
        reset_current_request_id(token)


def test_nested_request_context_resets_to_parent_binding() -> None:
    outer = set_current_request_id("req-outer")
    try:
        inner = set_current_request_id("req-inner")
        try:
            assert require_current_request_id("inner") == "req-inner"
        finally:
            reset_current_request_id(inner)
        assert require_current_request_id("outer") == "req-outer"
    finally:
        reset_current_request_id(outer)
    assert get_current_request_id() is None
