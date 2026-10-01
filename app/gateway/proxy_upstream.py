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

# httpx 按 RFC 3986 解析 path 组件：`#` 之后算 fragment、`?` 之后算 query，两者都
# 不能出现在 path 里，ASCII 控制字符同样非法。uvicorn 只对请求行解码一层，因此
# 文件名里的 `#`/`?`（客户端编码成 %23/%3F 发送）到达代理时已是字面量形态，必须
# 在这里重新编码，否则 httpx 抛 InvalidURL，合法文件名被代理解析成 500。
_PATH_COMPONENT_DELIMITER_ESCAPES = str.maketrans(
    {
        "#": "%23",
        "?": "%3F",
        **{chr(code): f"%{code:02X}" for code in range(0x20)},
        "\x7f": "%7F",
    }
)

# 代理两端（工作区 API 与辅助服务）必须剔除同一组逐跳头部，否则上游的
# 连接管理头部会漂到浏览器侧或被转发回上游。集合与过滤函数只此一份。
#
# 逐跳头部的完整定义有两部分，缺一不可：RFC 7230 6.1 的固定清单，以及由
# Connection 头部逐个点名的字段（Connection 值为 X-Internal 时，X-Internal
# 也是这条连接专属的）。只剔固定清单会让「被点名」的字段原样漂到对端；上游
# 用它标注内部/私有头部时会直接泄漏给浏览器。固定清单里的名字取 RFC 7230 的
# 规范拼写：Trailer（RFC 2616 的 Trailers 是历史拼写，不是真实头部名）。
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# 工作区 API 代理与辅助服务代理都要剥离的 Gateway 凭据与目标选择头部。它们绝不
# 能透传给上游，否则客户端可以伪造工作区选择或把本地凭据漂到被代理服务。集合与
# 逐跳头部一样只此一份，避免两条代理各写一份而漏掉其中一个。
GATEWAY_PROXY_DROPPED_HEADERS = frozenset(
    {
        "host",
        "x-request-id",
        "x-local-token",
        "x-boxteam-federation-token",
        "x-boxteam-workspace-id",
    }
)


def filter_hop_by_hop_headers(
    headers: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """按 RFC 7230 剔除逐跳头部，保留其余头部的原样顺序。

    逐跳头部 = 固定清单里的名字并上 Connection 头部点名的名字。点名的名字要
    按大小写不敏感比较，且可能出现在任意一条 Connection 里；因此先扫出全部
    Connection 值中的逗号分隔 token，再连同固定清单一并过滤。
    """
    normalized = list(headers)
    nominated: set[str] = set()
    for key, value in normalized:
        if key.lower() != "connection":
            continue
        nominated.update(
            token.strip().lower() for token in value.split(",") if token.strip()
        )
    return {
        key: value
        for key, value in normalized
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() not in nominated
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
    url = base.copy_with(
        path=f"{base_prefix}/{head}{path.lstrip('/')}".translate(
            _PATH_COMPONENT_DELIMITER_ESCAPES
        )
    )
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
