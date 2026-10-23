from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token


class TestProcessScope:
    def __init__(self) -> None:
        self._cleanup_by_handle_id: dict[int, tuple[object, Callable[[], None]]] = {}
        self._closed = False

    def register(self, handle: object, cleanup: Callable[[], None]) -> None:
        if self._closed:
            raise RuntimeError("测试进程生命周期 scope 已关闭")
        handle_id = id(handle)
        if handle_id in self._cleanup_by_handle_id:
            raise ValueError("测试进程在 scope 中重复注册")
        self._cleanup_by_handle_id[handle_id] = (handle, cleanup)

    def unregister(self, handle: object) -> None:
        handle_id = id(handle)
        registered = self._cleanup_by_handle_id.get(handle_id)
        if registered is not None and registered[0] is handle:
            del self._cleanup_by_handle_id[handle_id]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanups, self._cleanup_by_handle_id = (
            tuple(self._cleanup_by_handle_id.values()),
            {},
        )
        failures: list[BaseException] = []
        for _, cleanup in reversed(cleanups):
            try:
                cleanup()
            except BaseException as error:  # noqa: BLE001 — 中断也须回收其余已登记进程，随后保留原始错误。
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("测试进程回收失败", failures)


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
