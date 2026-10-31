"""操作 ID 严格拒绝非法输入，纠错消息与原始诊断分离。"""

from __future__ import annotations

from traceback import format_exception

import pytest

from app.schemas.internal_v2.session_navigation.operations import validate_operation_id


@pytest.mark.parametrize("value", [
    "/operation-id-private/secret", r"C:\operation-id-private\secret",
    r"\\operation-id-private\share\secret", "file:///operation-id-private/secret",
    "", "op_" + "a" * 31, "op_" + "a" * 33, "op_" + "A" * 32,
    " op_" + "a" * 32, "op_" + "a" * 32 + "\n",
])
def test_invalid_operation_id_retains_value_only_in_diagnostic(value: str) -> None:
    with pytest.raises(ValueError) as captured:
        validate_operation_id(value)
    assert str(captured.value) == "operation_id 形态非法：必须为 op_ 加 32 位小写十六进制字符"
    assert repr(value) in "".join(format_exception(captured.value))


@pytest.mark.parametrize("value", [None, 123, ["/operation-id-private/secret"], {"operation-id-private": "secret"}])
def test_non_string_operation_id_preserves_type_error(value: object) -> None:
    with pytest.raises(TypeError) as captured:
        validate_operation_id(value)
    assert str(captured.value) == "operation_id 必须是字符串"
    assert repr(value) in "".join(format_exception(captured.value))


@pytest.mark.parametrize("value", ["op_" + "0" * 32, "op_" + "abcdef0123456789" * 2])
def test_valid_operation_id_is_not_rewritten(value: str) -> None:
    assert validate_operation_id(value) is None
