"""Gateway 上游请求的统一发送入口。

Gateway 的两个 httpx 客户端都不设读取超时：SSE 必须能长期占用到工作区后端的连接。
响应头不属于流式传输，上游接受 TCP 后永不回写时浏览器请求会永久挂起，同时上游
连接与代理任务一起泄漏。工作区 API 与辅助服务代理共用这里的同一份响应头上限。

代理的两条边界也共用这里：上游 URL 的组装（必须留在目标前缀内）与上游响应在
客户端取消下的释放。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import unquote

import httpx

UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS = 60.0

# 代理两端（工作区 API 与辅助服务）必须剔除同一组逐跳头部，否则上游的
# 连接管理头部会漂到浏览器侧或被转发回上游。集合与过滤函数只此一份。
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def filter_hop_by_hop_headers(
    headers: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """按 RFC 7230 剔除逐跳头部，保留其余头部的原样顺序。"""
    return {
        key: value
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


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

    越界有两条来源，必须一起堵住：

    1. uvicorn 把请求行里的 %2e%2e 解码成 ..，httpx 随后按 RFC 3986 折叠点段，
       于是 /api/v1/%2e%2e/%2e%2e/api/gateway/health 折叠出 /api/gateway/health。
    2. uvicorn 对请求行只解码一层，%252e%252e 会被解成字面量 %2e%2e；httpx 不再
       二次解码、也就不会折叠，前缀校验看见的仍是 /api/v1/%2e%2e/...，于是放行。
       但上游（工作区后端与辅助服务都是 uvicorn）会再解码一层并折叠点段，请求
       照样落到上游自身命名空间之外。

    第 2 条要求「判断归属之前先把百分号编码归一」。这里反复 unquote 到不动点：
    每轮 unquote 都把 %XX 三个字节换成单个字节，字节长度严格递减，所以最多
    len(path) 轮就到达不动点，不存在无限解码，也不依赖对输入的任何形状假设。

    为什么不改成「解码后仍含 % 就拒绝」：文件名里的字面量百分号会被编码成 %25
    （如 100%25.txt → 100%.txt），它解码后仍含 %，一律拒绝会误杀合法文件；而不
    动点法只看最终形状里是否真的出现 .. 点段。

    点段在解码过程中只增不减（unquote 只替换 %XX，不动字面量点），所以不动点形
    式是解码链上「点段最多」的一端；只要它没有 .. 点段，上游无论解几层都构造不
    出 ..，前缀校验因此成立。校验通过后仍按原始编码路径转发，保证 docs%2Fa.png
    这类「编码斜杠」路径的分段语义不被改写。
    """

    decoded = path
    while True:
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if ".." in decoded.split("/"):
        raise ValueError(
            "Gateway 代理路径归一后含 .. 点段: "
            f"path={path!r}, 归一结果={decoded!r}"
        )

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
