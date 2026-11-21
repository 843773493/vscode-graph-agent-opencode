"""SDK scope 与产品同步 worker 的真实 I/O 关闭协作。"""

from __future__ import annotations

import asyncio
import os
from threading import Event, get_ident

import pytest
from boxteam_plugin_sdk.lifecycle import LifetimeScope

from app.core.shielded_cleanup import run_sync_shielded


@pytest.mark.asyncio
async def test_scope_drains_cancelled_sync_io_worker_before_releasing_dependencies() -> None:
    read_fd, write_fd = os.pipe()
    entered = Event()
    worker_finished = Event()
    failure = OSError("late worker I/O failure")
    scope = LifetimeScope("shielded-worker")
    order: list[str] = []
    loop_thread = get_ident()
    task = None
    waiters: list[asyncio.Task[None]] = []

    def read_then_fail() -> None:
        assert get_ident() != loop_thread
        entered.set()
        try:
            assert os.read(read_fd, 1) == b"x"
            raise failure
        finally:
            worker_finished.set()

    async def worker() -> None:
        await run_sync_shielded(read_then_fail)

    def release_dependency() -> None:
        assert worker_finished.is_set()
        order.append("dependency-released")

    try:
        task = scope.start_task(worker, label="periodic-io")
        scope.register(release_dependency, label="io-dependency")
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
        waiters = [asyncio.create_task(scope.close()) for _ in range(3)]
        async with asyncio.timeout(2):
            while task.cancelling() == 0:
                await asyncio.sleep(0)
        assert not worker_finished.is_set() and order == []

        waiters[0].cancel("仅取消此关闭观察者")
        with pytest.raises(asyncio.CancelledError, match="仅取消此关闭观察者"):
            await waiters[0]
        assert not worker_finished.is_set() and order == []

        os.write(write_fd, b"x")
        results = await asyncio.gather(*waiters[1:], return_exceptions=True)
        assert worker_finished.is_set()
        assert order == ["dependency-released"]
        assert all(isinstance(result, ExceptionGroup) for result in results)
        assert results[0] is results[1]
        assert results[0].exceptions[0].__cause__ is failure

        await scope.close()
        assert scope.is_closed
    finally:
        # 即使断言提前失败，EOF 也会释放阻塞在 read 上的 worker。
        os.close(write_fd)
        if task is not None and not scope.is_closed:
            await asyncio.wait_for(
                asyncio.gather(scope.close(), return_exceptions=True), timeout=3,
            )
        await asyncio.gather(*waiters, return_exceptions=True)
        os.close(read_fd)
