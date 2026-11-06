"""宿主通道只传原协议字节；任一方向结束后结清同一组任务。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine

from starlette.websockets import WebSocket, WebSocketState

from app.core.shielded_cleanup import run_cleanup_shielded


async def close_channel(websocket: WebSocket, *, code: int = 1000, reason: str = "") -> None:
    # 已收到 disconnect 时，ASGI server 已完成关闭握手，不能再发送第二个 close。
    if websocket.client_state == WebSocketState.CONNECTED and websocket.application_state == WebSocketState.CONNECTED:
        await websocket.close(code=code, reason=reason)


async def run_channel(*operations: Coroutine[None, None, None]) -> None:
    tasks = [asyncio.create_task(operation) for operation in operations]
    failure: BaseException | None = None
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except BaseException as error:  # noqa: BLE001 - 原取消也须在同组任务结清后传播。
        failure = error

    async def settle() -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)
                  and not isinstance(result, asyncio.CancelledError)]
        if failure is not None:
            errors.insert(0, failure)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Node 宿主通道及收尾失败", errors)

    await run_cleanup_shielded(settle)
