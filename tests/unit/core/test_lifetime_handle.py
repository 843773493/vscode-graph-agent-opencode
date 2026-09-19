"""LifetimeScope 可撤销 handle（OpenSpec 3.9）的登记/撤销/关闭合同测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.lifecycle import LifetimeScope


@pytest.mark.asyncio
async def test_revoke_prevents_callback_from_running_again_at_close() -> None:
    calls: list[str] = []
    scope = LifetimeScope("handle")
    handle = scope.register(lambda: calls.append("released"), label="sub")
    assert handle.resource_id >= 0
    assert handle.label == "sub"
    assert handle.revoked is False

    # 撤销 = 提前释放：回调在撤销时执行一次。
    await handle.revoke()
    assert handle.revoked is True
    assert calls == ["released"]

    # 关闭不再重复调用该回调。
    await scope.close()
    assert calls == ["released"]
    assert scope.snapshot().resource_count == 0


@pytest.mark.asyncio
async def test_revoke_is_idempotent_and_callback_runs_once() -> None:
    calls: list[str] = []
    scope = LifetimeScope("idempotent")
    handle = scope.register(lambda: calls.append("released"), label="sub")

    await handle.revoke()
    await handle.revoke()
    await handle.revoke()
    assert calls == ["released"]
    await scope.close()
    assert calls == ["released"]


@pytest.mark.asyncio
async def test_revoke_runs_async_callback() -> None:
    calls: list[str] = []
    scope = LifetimeScope("async-callback")

    async def release() -> None:
        await asyncio.sleep(0)
        calls.append("released")

    handle = scope.register(release, label="sub")
    await handle.revoke()
    assert calls == ["released"]


@pytest.mark.asyncio
async def test_register_after_close_is_rejected() -> None:
    scope = LifetimeScope("closed")
    await scope.close()
    with pytest.raises(RuntimeError, match="不允许注册"):
        scope.register(lambda: None, label="late")


@pytest.mark.asyncio
async def test_revoke_after_successful_close_is_noop() -> None:
    calls: list[str] = []
    scope = LifetimeScope("after-close")
    handle = scope.register(lambda: calls.append("released"), label="sub")
    await scope.close()
    assert calls == ["released"]
    # 成功关闭后资源必然已释放；再次撤销退化为无操作，不报错。
    await handle.revoke()
    assert calls == ["released"]


@pytest.mark.asyncio
async def test_release_by_resource_id_still_works() -> None:
    """release(resource_id) 保留：经 handle.resource_id 提前释放等效。"""
    calls: list[str] = []
    scope = LifetimeScope("release-by-id")
    handle = scope.register(lambda: calls.append("released"), label="sub")

    await scope.release(handle.resource_id)
    assert calls == ["released"]
    # 重复 release 同一 resource_id 幂等。
    await scope.release(handle.resource_id)
    assert calls == ["released"]
    await scope.close()
    assert calls == ["released"]


@pytest.mark.asyncio
async def test_revoke_failure_keeps_handle_retryable() -> None:
    calls = 0

    def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("still attached")

    scope = LifetimeScope("retry-revoke")
    handle = scope.register(flaky, label="sub")
    with pytest.raises(OSError, match="still attached"):
        await handle.revoke()
    # 撤销失败不吞错也不虚报成功；句柄保持未撤销，可重试。
    assert handle.revoked is False
    await handle.revoke()
    assert calls == 2
    assert handle.revoked is True
