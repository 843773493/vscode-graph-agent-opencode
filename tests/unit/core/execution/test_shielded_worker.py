"""原生worker结算与调用者取消之间不能丢失取消，也不能遗弃原worker。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest

from app.core.shielded_cleanup import run_sync_shielded

T = TypeVar("T")


@pytest.fixture
def cancel_after_worker_result(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[], bool]], list[bool]]:
    shield = asyncio.shield
    callbacks: list[Callable[[], bool]] = []
    requests: list[bool] = []

    def observe(awaitable: Awaitable[T]) -> asyncio.Future[T]:
        result = shield(awaitable)
        if callbacks:
            assert isinstance(awaitable, asyncio.Future)
            cancel = callbacks.pop()
            loop = asyncio.get_running_loop()

            def request(_done: asyncio.Future[T]) -> None:
                # 原shield先登记结果回调：先唤醒调用者，再交付外部取消。
                loop.call_soon(lambda: requests.append(cancel()))

            awaitable.add_done_callback(request)
        return result

    def arm(cancel: Callable[[], bool]) -> list[bool]:
        callbacks.append(cancel)
        return requests

    monkeypatch.setattr(asyncio, "shield", observe)
    return arm


@pytest.mark.parametrize("wait_after_result", [False, True], ids=["return", "continue-waiting"])
async def test_successful_worker_cannot_swallow_an_accepted_cancellation(
    cancel_after_worker_result: Callable[[Callable[[], bool]], list[bool]], wait_after_result: bool,
) -> None:
    idle = asyncio.Event()

    async def caller() -> int:
        value = await run_sync_shielded(lambda: 42)
        if wait_after_result:
            await idle.wait()
        return value

    task = asyncio.create_task(caller())
    requests = cancel_after_worker_result(lambda: task.cancel("原生结果结算后的外部取消"))
    try:
        done, _pending = await asyncio.wait({task}, timeout=1)
        assert done == {task}, "已接受的取消被吞掉，调用者继续等待后续工作"
        if requests == [True]:
            with pytest.raises(asyncio.CancelledError, match="原生结果结算后的外部取消"):
                await task
        else:
            # 直接返回可能先于取消请求完成；此时cancel必须明确返回False。
            assert requests == [False]
            assert task.result() == 42 and task.cancelling() == 0
    finally:
        if not task.done():
            task.cancel("测试收尾")
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("native_failure", [False, True])
async def test_cancelled_waiter_keeps_original_lock_until_native_worker_settles(native_failure: bool) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    lock = asyncio.Lock()
    native = OSError(28, "原生worker写盘失败")
    calls: list[int] = []

    def operation() -> int:
        calls.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            raise TimeoutError("测试worker屏障未释放")
        if native_failure:
            raise native
        return 42

    async def caller() -> int:
        async with lock:
            return await run_sync_shielded(operation)

    task = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("原调用取消")
        await asyncio.sleep(0)
        assert lock.locked() and not task.done()
        task.cancel("重复取消")
        await asyncio.sleep(0)
        assert lock.locked() and not task.done()
        release.set()
        done, _pending = await asyncio.wait({task}, timeout=1)
        assert done == {task}
        if native_failure:
            with pytest.raises(OSError) as caught:
                await task
            assert caught.value is native and native.errno == 28
            assert isinstance(native.__context__, asyncio.CancelledError)
            assert str(native.__context__) == "原调用取消"
        else:
            with pytest.raises(asyncio.CancelledError, match="原调用取消"):
                await task
        assert not lock.locked() and len(calls) == 1
        assert calls[0] != threading.get_ident()
    finally:
        release.set()
        if not task.done():
            task.cancel("测试收尾")
        await asyncio.gather(task, return_exceptions=True)


async def test_worker_preserves_arguments_result_and_native_failure() -> None:
    calls: list[tuple[int, str]] = []
    native = PermissionError(13, "原生访问拒绝")

    def operation(value: int, *, mode: str) -> int:
        calls.append((value, mode))
        if mode == "fail":
            raise native
        return value * 2

    assert await run_sync_shielded(operation, 21, mode="return") == 42
    with pytest.raises(PermissionError) as caught:
        await run_sync_shielded(operation, 7, mode="fail")
    assert caught.value is native
    assert calls == [(21, "return"), (7, "fail")]
