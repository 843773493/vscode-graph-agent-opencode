from __future__ import annotations

import pytest

from tests.support.processes import scope as process_scope


@pytest.mark.parametrize("interruption", [KeyboardInterrupt("停止"), SystemExit(3)])
def test_cleanup_interruption_does_not_skip_remaining_processes(
    interruption: BaseException,
) -> None:
    scope = process_scope.TestProcessScope()
    order = []
    first_error = ValueError("首个进程清理失败")

    def first() -> None:
        order.append("first")
        raise first_error

    def second() -> None:
        order.append("second")
        raise interruption

    scope.register(object(), first)
    scope.register(object(), second)
    failure = None
    try:
        scope.close()
    except BaseException as error:  # noqa: BLE001 - 回归时也须捕获被测中断，避免中断整轮 pytest。
        failure = error

    assert order == ["second", "first"]
    assert isinstance(failure, BaseExceptionGroup)
    assert failure.exceptions == (interruption, first_error)
    scope.close()
    assert order == ["second", "first"]


def test_cleanup_failure_retains_body_error_and_restores_outer_scope() -> None:
    order = []
    body_error = ValueError("测试主体失败")
    cleanup_error = OSError("进程关闭失败")

    def fail_cleanup() -> None:
        order.append("inner")
        raise cleanup_error

    with process_scope.capture_test_processes():
        process_scope.register_test_process(object(), lambda: order.append("outer-first"))
        with (
            pytest.raises(ExceptionGroup) as failure,
            process_scope.capture_test_processes(),
        ):
            process_scope.register_test_process(object(), fail_cleanup)
            raise body_error
        assert failure.value.exceptions == (cleanup_error,)
        assert failure.value.__context__ is body_error
        process_scope.register_test_process(object(), lambda: order.append("outer-last"))

    assert order == ["inner", "outer-last", "outer-first"]


def test_closed_scope_reclaims_late_handle_and_preserves_cleanup_failure() -> None:
    scope = process_scope.TestProcessScope()
    scope.close()
    cleaned = []
    with pytest.raises(RuntimeError, match="scope 已关闭"):
        scope.register(object(), lambda: cleaned.append("late"))
    assert cleaned == ["late"]
    cleanup_error = OSError("迟到进程回收失败")

    def fail_cleanup() -> None:
        raise cleanup_error

    with pytest.raises(ExceptionGroup) as failure:
        scope.register(object(), fail_cleanup)
    assert isinstance(failure.value.exceptions[0], RuntimeError)
    assert failure.value.exceptions[1] is cleanup_error


def test_duplicate_registration_does_not_close_the_existing_owner() -> None:
    scope = process_scope.TestProcessScope()
    handle = object()
    cleaned = []
    scope.register(handle, lambda: cleaned.append("owner"))
    with pytest.raises(ValueError, match="重复注册"):
        scope.register(handle, lambda: cleaned.append("duplicate"))
    assert not cleaned
    scope.close()
    assert cleaned == ["owner"]
