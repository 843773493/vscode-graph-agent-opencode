from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token


class TestProcessScope:
    def __init__(self) -> None:
        self._cleanups: list[tuple[object, Callable[[], None]]] = []
        self._closed = False

    def register(self, handle: object, cleanup: Callable[[], None]) -> None:
        if self._closed:
            raise RuntimeError("测试进程生命周期 scope 已关闭")
        if any(tracked_handle is handle for tracked_handle, _ in self._cleanups):
            raise ValueError("测试进程在 scope 中重复注册")
        self._cleanups.append((handle, cleanup))

    def unregister(self, handle: object) -> None:
        self._cleanups = [
            item for item in self._cleanups if item[0] is not handle
        ]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanups, self._cleanups = self._cleanups, []
        failures: list[Exception] = []
        for _, cleanup in reversed(cleanups):
            try:
                cleanup()
            except Exception as error:  # noqa: BLE001 — 必须回收全部已登记进程并统一报告清理失败。
                failures.append(error)
        if failures:
            raise ExceptionGroup("测试进程回收失败", failures)


_ACTIVE_TEST_PROCESS_SCOPE: ContextVar[TestProcessScope | None] = ContextVar(
    "boxteam_test_process_scope",
    default=None,
)


@contextmanager
def capture_test_processes() -> Iterator[None]:
    scope = TestProcessScope()
    token: Token[TestProcessScope | None] = _ACTIVE_TEST_PROCESS_SCOPE.set(scope)
    try:
        yield
    finally:
        try:
            scope.close()
        finally:
            _ACTIVE_TEST_PROCESS_SCOPE.reset(token)


def register_test_process(handle: object, cleanup: Callable[[], None]) -> None:
    scope = _ACTIVE_TEST_PROCESS_SCOPE.get()
    if scope is not None:
        scope.register(handle, cleanup)


def unregister_test_process(handle: object) -> None:
    scope = _ACTIVE_TEST_PROCESS_SCOPE.get()
    if scope is not None:
        scope.unregister(handle)
