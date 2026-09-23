"""Gateway 上游请求的统一发送入口。

Gateway 的两个 httpx 客户端都不设读取超时：SSE 必须能长期占用到工作区后端的连接。
响应头不属于流式传输，上游接受 TCP 后永不回写时浏览器请求会永久挂起，同时上游
连接与代理任务一起泄漏。工作区 API 与辅助服务代理共用这里的同一份响应头上限。
"""

from __future__ import annotations

import asyncio

import httpx

UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS = 60.0


async def send_upstream_request(
    client: httpx.AsyncClient,
    request: httpx.Request,
) -> httpx.Response:
    """发送上游请求，只约束响应头等待，不约束响应体的流式读取。

    超时由 :func:`asyncio.wait_for` 取消 ``send``；httpcore 在取消时会把该请求移出
    连接池并关闭已建立的上游连接，因此挂起的上游不会随代理任务一起泄漏。
    """

    return await asyncio.wait_for(
        client.send(request, stream=True),
        timeout=UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS,
    )
