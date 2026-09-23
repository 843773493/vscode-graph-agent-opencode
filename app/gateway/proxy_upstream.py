"""Gateway 上游请求的统一发送入口。

Gateway 的两个 httpx 客户端都不设读取超时：SSE 必须能长期占用到工作区后端的连接。
响应头不属于流式传输，上游接受 TCP 后永不回写时浏览器请求会永久挂起，同时上游
连接与代理任务一起泄漏。工作区 API 与辅助服务代理共用这里的同一份响应头上限。

代理的两条边界也共用这里：上游 URL 的组装（必须留在目标前缀内）与上游响应在
客户端取消下的释放。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

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


def build_upstream_url(
    base_url: str,
    fixed_segments: tuple[str, ...],
    path: str,
) -> httpx.URL:
    """把代理路径拼到上游 base URL 的固定命名空间下，越界即响亮失败。

    uvicorn 会把请求行里的 %2e%2e 解码成 ..，httpx 随后按 RFC 3986 折叠点段；
    两者叠加会让 /api/v1/%2e%2e/%2e%2e/api/gateway/health 这类路径折叠出
    /api/gateway/health，静默逃出被代理服务的命名空间，并带上 Gateway 为远程
    目标附加的联邦凭据。这里用 httpx 的同一套规范化结果校验结果仍属于
    「base_url 路径 + fixed_segments」这一前缀，绝不把脏路径当有效路径继续转发。
    """

    base = httpx.URL(base_url)
    base_prefix = base.path.rstrip("/")
    namespace = "/".join(fixed_segments)
    head = f"{namespace}/" if namespace else ""
    url = base.copy_with(path=f"{base_prefix}/{head}{path.lstrip('/')}")
    allowed_prefix = f"{base_prefix}/{head}" if head else f"{base_prefix}/"
    if not url.path.startswith(allowed_prefix):
        raise ValueError(
            "Gateway 代理路径越出上游命名空间: "
            f"allowed_prefix={allowed_prefix!r}, path={path!r}, "
            f"规范化结果={url.path!r}"
        )
    return url


async def run_cleanup_shielded(cleanup: Callable[[], Awaitable[None]]) -> None:
    """在取消已经发生的情况下把清理跑完，再由调用方继续传播取消。

    uvicorn 声明 ASGI spec 2.3，starlette 的 StreamingResponse 用 anyio task
    group 驱动响应体迭代；客户端断开时 cancel scope 取消流任务，此后流生成器
    finally 中每个 await 都会立刻重新抛出 CancelledError。把
    await response.aclose() 直接写在 finally 里会被这第二次取消打断：上游连接
    不关闭、路由引用计数不归零，工作区重启或删除随后会被「仍有代理引用」永久挡住。

    这里把清理放进独立 task 并反复 shield 它直到真正完成。每次取消只能打断
    await，打不断那个独立 task；清理自身失败会在此响亮抛出，不静默吞掉。
    """

    cleanup_task = asyncio.ensure_future(cleanup())
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    cleanup_task.result()
