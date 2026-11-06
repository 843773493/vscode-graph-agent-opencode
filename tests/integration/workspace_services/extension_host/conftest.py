"""扩展组件的真实 HTTP 装具；不装配完整工作区业务图或开发服务。"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack

import pytest
import uvicorn
from fastapi import FastAPI


@pytest.fixture
async def network_scope() -> AsyncIterator[AsyncExitStack]:
    async with AsyncExitStack() as scope:
        yield scope


@pytest.fixture
def serve_extension_application(network_scope: AsyncExitStack) -> Callable[[FastAPI], Awaitable[str]]:
    async def serve(application: FastAPI) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        network_scope.callback(listener.close)
        server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
        task = asyncio.create_task(server.serve(sockets=[listener]))

        async def close() -> None:
            server.should_exit = True
            async with asyncio.timeout(10):
                await task

        network_scope.push_async_callback(close)
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                    raise RuntimeError("真实扩展组件 HTTP 服务在就绪前退出")
                await asyncio.sleep(0.01)
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    return serve
