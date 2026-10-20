from __future__ import annotations

import pytest

from tests.support import process_scope


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
