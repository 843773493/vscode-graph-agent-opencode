"""双向任务结束、取消与同时失败都必须结清原任务。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.infrastructure.extension_host.channel import run_channel


async def test_first_eof_waits_for_other_direction_cleanup() -> None:
    entered, exited = asyncio.Event(), asyncio.Event()

    async def peer() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            exited.set()

    async def eof() -> None:
        await entered.wait()

    await run_channel(peer(), eof())
    assert exited.is_set()


async def test_cancel_preserves_cleanup_failure() -> None:
    entered, exited = asyncio.Event(), asyncio.Event()
    failure = OSError("原连接收尾失败")

    async def peer() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
            raise failure

    channel = asyncio.create_task(run_channel(peer()))
    await entered.wait()
    channel.cancel()
    with pytest.raises(BaseExceptionGroup) as errors:
        await channel
    assert exited.is_set()
    assert any(isinstance(error, asyncio.CancelledError) for error in errors.value.exceptions)
    assert failure in errors.value.exceptions


async def test_simultaneous_failures_preserve_both_causes() -> None:
    failures = (ValueError("上行失败"), OSError("下行失败"))

    async def fail(error: Exception) -> None:
        raise error

    with pytest.raises(ExceptionGroup) as errors:
        await run_channel(*(fail(error) for error in failures))
    assert errors.value.exceptions == failures
