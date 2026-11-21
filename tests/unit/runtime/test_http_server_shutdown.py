"""领域关闭失败不能跳过 Uvicorn 的监听、连接及 ASGI lifespan 收尾。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
import uvicorn

from app.runtime.http_server import ManagedHTTPServer


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    draining = AsyncMock()
    monkeypatch.setattr(uvicorn.Server, "shutdown", draining)
    server = ManagedHTTPServer(uvicorn.Config("app.main:app"))
    server.started = True
    server.lifespan = SimpleNamespace(state={})
    return server, draining


async def test_shutdown_orders_domain_cleanup_before_http_drain(server) -> None:
    service, draining = server
    closing = AsyncMock()
    ordering = Mock()
    ordering.attach_mock(closing, "closing")
    ordering.attach_mock(draining, "draining")
    service.lifespan.state["before_http_shutdown"] = closing
    await service.shutdown()
    closing.assert_awaited_once_with()
    draining.assert_awaited_once_with(sockets=None)
    assert ordering.mock_calls == [call.closing(), call.draining(sockets=None)]


async def test_owner_failure_still_waits_for_started_initialization(server) -> None:
    service, draining = server
    finished = asyncio.Event()

    async def initialize() -> None:
        await asyncio.sleep(0)
        finished.set()

    service.http_startup_task = asyncio.create_task(initialize())
    original = RuntimeError("关闭失败")
    service.lifespan.state["before_http_shutdown"] = AsyncMock(side_effect=original)
    with pytest.raises(RuntimeError) as caught:
        await service.shutdown()
    assert caught.value is original
    assert finished.is_set()
    draining.assert_awaited_once_with(sockets=None)


@pytest.mark.parametrize("cancelled", [False, True], ids=["owner-failure", "cancelled"])
async def test_owner_failure_still_drains_and_preserves_original_error(server, cancelled: bool) -> None:
    service, draining = server
    original = asyncio.CancelledError() if cancelled else RuntimeError("Backend 退出未核实")
    service.lifespan.state["before_http_shutdown"] = AsyncMock(side_effect=original)
    sockets = []
    with pytest.raises(type(original)) as caught:
        await service.shutdown(sockets=sockets)
    assert caught.value is original
    draining.assert_awaited_once_with(sockets=sockets)


async def test_shutdown_keeps_both_owner_and_http_failures(server) -> None:
    service, draining = server
    owner_error, drain_error = RuntimeError("Backend 关闭失败"), OSError("监听关闭失败")
    service.lifespan.state["before_http_shutdown"] = AsyncMock(side_effect=owner_error)
    draining.side_effect = drain_error
    with pytest.raises(ExceptionGroup) as caught:
        await service.shutdown()
    assert caught.value.exceptions == (owner_error, drain_error)
    draining.assert_awaited_once_with(sockets=None)
