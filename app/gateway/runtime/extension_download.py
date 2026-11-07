"""原安装请求的目标 HTTP 传输；不选择版本、不写包或维护安装状态。"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.proxy_upstream import (
    GATEWAY_PROXY_DROPPED_HEADERS,
    filter_hop_by_hop_headers,
)

DOWNLOAD_ERROR_HEADER = "x-boxteam-extension-download-error"
logger = logging.getLogger(__name__)
_private_transport: ContextVar[bool] = ContextVar("extension_download_private_transport", default=False)


class _DownloadLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 依赖的 wire 日志含签名 URL、代理及响应头；仅屏蔽本次下载的原始细节。
        return not _private_transport.get()


_download_log_filter = _DownloadLogFilter()
for _logger_name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_logger_name).addFilter(_download_log_filter)


@contextmanager
def _private_transport_logging() -> Iterator[None]:
    token = _private_transport.set(True)
    try:
        yield
    finally:
        _private_transport.reset(token)


DownloadCallSite = Literal[
    "extensionGalleryService.download", "extensionGalleryService.download.fallback",
    "extensionGalleryService.signature", "extensionGalleryService.signature.fallback",
    "extensionManagement.downloadVsix",
]


class ExtensionDownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    call_site: DownloadCallSite
    url: HttpUrl
    headers: dict[str, str | list[str]] = Field(default_factory=dict)
    timeout_ms: float | None = Field(default=None, ge=0)
    follow_redirects: int = Field(default=3, ge=0)
    proxy_url: HttpUrl | None = None
    strict_ssl: bool


def _headers(raw: list[tuple[str, str]]) -> list[tuple[str, str]]:
    # 复用原逐跳头规则，同时保留上游的重复响应头。
    allowed = filter_hop_by_hop_headers(raw)
    return [(name, value) for name, value in raw if name in allowed
        and name.lower() not in GATEWAY_PROXY_DROPPED_HEADERS
        and name.lower() != DOWNLOAD_ERROR_HEADER]


class _DownloadResponse(StreamingResponse):
    def __init__(self, response: httpx.Response, resources: AsyncExitStack) -> None:
        # 原 nodeRequest 负责解压；此处不能把解压正文与原压缩长度／编码混在一起。
        super().__init__(response.aiter_raw(), status_code=response.status_code)
        self.raw_headers = [(name.encode("latin-1"), value.encode("latin-1"))
            for name, value in _headers(list(response.headers.multi_items()))]
        self._resources = resources

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with _private_transport_logging():
            try:
                await super().__call__(scope, receive, send)
            finally:
                # 包括首次迭代前断连；不把连接释放责任仅交给正文生成器。
                await run_cleanup_shielded(self._resources.aclose)


async def extension_download(payload: ExtensionDownloadRequest) -> StreamingResponse:
    raw = [(name, value) for name, values in payload.headers.items()
        for value in (values if isinstance(values, list) else [values])]
    proxy_authorization = [value for name, value in raw if name.lower() == "proxy-authorization"]
    if len(proxy_authorization) > 1 or proxy_authorization and payload.proxy_url is None:
        raise ValueError("扩展下载代理认证必须绑定唯一的明确代理")
    proxy = None
    if payload.proxy_url is not None:
        proxy_ssl = None
        if payload.proxy_url.scheme == "https":
            proxy_ssl = ssl.create_default_context()
            if not payload.strict_ssl:
                proxy_ssl.check_hostname = False
                proxy_ssl.verify_mode = ssl.CERT_NONE
        proxy = httpx.Proxy(str(payload.proxy_url), ssl_context=proxy_ssl,
            headers={"Proxy-Authorization": proxy_authorization[0]} if proxy_authorization else None)
    resources = AsyncExitStack()
    with _private_transport_logging():
        return await _download(payload, proxy, raw, resources)


async def _download(payload: ExtensionDownloadRequest, proxy: httpx.Proxy | None,
                    raw: list[tuple[str, str]], resources: AsyncExitStack) -> StreamingResponse:
    try:
        # 配置及 resolved shell env 的代理选择来自原安装服务，不能再套 Python 环境代理。
        client = await resources.enter_async_context(httpx.AsyncClient(proxy=proxy,
            verify=payload.strict_ssl, trust_env=False,
            timeout=payload.timeout_ms / 1000 if payload.timeout_ms else None))
        # Node 仅解 gzip；不能让 httpx 默认协商 br/deflate 等编码后把原始流交给它。
        client.headers.clear()
        request = client.build_request("GET", str(payload.url), headers=_headers(raw))
        response = await client.send(request, stream=True)
        for _ in range(payload.follow_redirects):
            if not 300 <= response.status_code < 400 or "Location" not in response.headers:
                break
            # 与原 nodeRequest 一致：只替换 URL，保留调用方原头，不吸收中间响应 Cookie。
            following_url = response.url.join(response.headers["Location"])
            await response.aclose()
            client.cookies.clear()
            following = client.build_request("GET", following_url, headers=_headers(raw))
            response = await client.send(following, stream=True)
        # 原预算耗尽后仍返回3xx，由原安装器决定失败或fallback，不另建重试策略。
        resources.push_async_callback(response.aclose)
        logger.info("目标扩展下载: call_site=%s host=%s status=%s", payload.call_site, payload.url.host, response.status_code)
        return _DownloadResponse(response, resources)
    except BaseException as error:
        logger.warning("目标扩展下载失败: call_site=%s host=%s error=%s", payload.call_site, payload.url.host, type(error).__name__)
        await run_cleanup_shielded(resources.aclose)
        raise
