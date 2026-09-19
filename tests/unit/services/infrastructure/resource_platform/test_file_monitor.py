"""SharedFileMonitor 完整 watch key 共享、引用计数与释放合同的单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.infrastructure.resource_platform.adapters.file_monitor import (
    FileMonitorBatch,
    FileMonitorKey,
    SharedFileMonitor,
)


class _FakeWatchPort:
    """记录 watch key 并允许测试定点投递 batch 的替身端口。"""

    def __init__(self) -> None:
        self.watch_keys: list[FileMonitorKey] = []
        self.closed_keys: set[FileMonitorKey] = set()
        self._queues: dict[FileMonitorKey, asyncio.Queue[FileMonitorBatch]] = {}

    def watch(self, key: FileMonitorKey):
        self.watch_keys.append(key)
        queue: asyncio.Queue[FileMonitorBatch] = asyncio.Queue()
        self._queues[key] = queue
        return self._iterate(key, queue)

    async def _iterate(self, key: FileMonitorKey, queue: asyncio.Queue[FileMonitorBatch]):
        try:
            while True:
                batch = await queue.get()
                yield batch
        finally:
            self.closed_keys.add(key)

    def emit(self, key: FileMonitorKey, batch: FileMonitorBatch) -> None:
        self._queues[key].put_nowait(batch)


def _key(correlation: str = "unit") -> FileMonitorKey:
    return FileMonitorKey(locator=str(Path("/tmp").absolute()), correlation=correlation)


@pytest.mark.asyncio
async def test_same_key_shares_single_subscription_and_fans_out() -> None:
    """相同完整 key 的两个 consumer 共享一条底层订阅并都收到事件。"""
    port = _FakeWatchPort()
    monitor = SharedFileMonitor(port=port, instance_id="m1")
    handle_a = await monitor.subscribe(_key())
    await asyncio.sleep(0.01)
    handle_b = await monitor.subscribe(_key())
    await asyncio.sleep(0.01)
    assert len(port.watch_keys) == 1

    received: list[FileMonitorBatch] = []

    async def _consume(handle) -> None:
        async for batch in handle.batches():
            received.append(batch)

    consumer_task = asyncio.create_task(_consume(handle_a))
    await asyncio.sleep(0)
    port.emit(_key(), FileMonitorBatch(changes=()))
    await asyncio.sleep(0.05)
    assert len(received) == 1
    consumer_task.cancel()

    await handle_a.release()
    # 还剩一个引用：底层订阅保持存活。
    await asyncio.sleep(0.05)
    assert _key() not in port.closed_keys
    await handle_b.release()
    await asyncio.sleep(0.05)
    # 最后一个引用释放后底层订阅关闭。
    assert _key() in port.closed_keys


@pytest.mark.asyncio
async def test_different_watch_semantics_do_not_share() -> None:
    """correlation 等任何 key 字段不同都不允许共享底层订阅。"""
    port = _FakeWatchPort()
    monitor = SharedFileMonitor(port=port, instance_id="m1")
    await monitor.subscribe(_key("skill"))
    await asyncio.sleep(0.01)
    await monitor.subscribe(_key("config"))
    await asyncio.sleep(0.01)
    assert len(port.watch_keys) == 2


@pytest.mark.asyncio
async def test_monitor_instance_isolates_sharing() -> None:
    """monitor instance 参与共享 key：两个实例互不共享订阅。"""
    port_a = _FakeWatchPort()
    port_b = _FakeWatchPort()
    monitor_a = SharedFileMonitor(port=port_a, instance_id="ma")
    monitor_b = SharedFileMonitor(port=port_b, instance_id="mb")
    await monitor_a.subscribe(_key())
    await asyncio.sleep(0.01)
    await monitor_b.subscribe(_key())
    await asyncio.sleep(0.01)
    assert len(port_a.watch_keys) == 1
    assert len(port_b.watch_keys) == 1


@pytest.mark.asyncio
async def test_overflow_is_visible_not_silent() -> None:
    """consumer 队列溢出时必须收到显式 overflow 标记。"""
    port = _FakeWatchPort()
    monitor = SharedFileMonitor(port=port, instance_id="m1")
    handle = await monitor.subscribe(_key())
    await asyncio.sleep(0.01)
    received: list[FileMonitorBatch] = []

    async def _consume() -> None:
        async for batch in handle.batches():
            received.append(batch)

    # 先灌满队列再启动 consumer：溢出标记的生成不依赖调度时序。
    for _index in range(64):
        port.emit(_key(), FileMonitorBatch(changes=()))
    consumer_task = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)
    consumer_task.cancel()
    assert any(batch.overflow for batch in received)
    await handle.release()


@pytest.mark.asyncio
async def test_closed_monitor_rejects_new_subscription() -> None:
    """monitor 关闭后拒绝新订阅；重复 close 幂等。"""
    port = _FakeWatchPort()
    monitor = SharedFileMonitor(port=port, instance_id="m1")
    handle = await monitor.subscribe(_key())
    await asyncio.sleep(0.01)
    await handle.release()
    await monitor.close()
    await monitor.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        await monitor.subscribe(_key())


def test_watch_key_validates_full_semantics() -> None:
    """key 校验覆盖 locator/correlation/filter 与排序约束。"""
    with pytest.raises(ValueError, match="绝对路径"):
        FileMonitorKey(locator="relative/path", correlation="x")
    with pytest.raises(ValueError, match="correlation"):
        FileMonitorKey(locator="/tmp", correlation=" ")
    with pytest.raises(ValueError, match="filter"):
        FileMonitorKey(locator="/tmp", correlation="x", filter_name="unknown")
    with pytest.raises(ValueError, match="exclude"):
        FileMonitorKey(locator="/tmp", correlation="x", exclude=("b", "a"))


def test_workspace_watch_port_rejects_unsupported_semantics(tmp_path: Path) -> None:
    """底层服务不支持的非递归/自定义 exclude 显式失败，不悄悄降级。"""
    from app.services.infrastructure.resource_platform.adapters.file_monitor import (
        WorkspaceFileWatchPort,
    )
    from app.services.infrastructure.workspace_file_watch_service import (
        WorkspaceFileWatchService,
    )

    service = WorkspaceFileWatchService(workspace_root=tmp_path)
    port = WorkspaceFileWatchPort(service)
    import asyncio

    async def _expect_reject(key: FileMonitorKey, message: str) -> None:
        iterator = port.watch(key)
        with pytest.raises(ValueError, match=message):
            await anext(iterator)

    asyncio.run(
        _expect_reject(
            FileMonitorKey(locator=str(tmp_path), correlation="x", recursive=False),
            "递归",
        )
    )
    asyncio.run(
        _expect_reject(
            FileMonitorKey(
                locator=str(tmp_path),
                correlation="x",
                exclude=("node_modules",),
            ),
            "exclude",
        )
    )
