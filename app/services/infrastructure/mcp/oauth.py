"""官方 OAuthClientProvider 的产品边界：支持确认、私有凭据和安全错误。"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from pydantic import AnyUrl

from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.infrastructure.mcp.oauth_credentials import McpOAuthCredentialStore
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata


class McpOAuthError(RuntimeError):
    """可进入普通诊断的安全 OAuth 错误；不复制交换正文或授权参数。"""


class McpAuthorizationRequired(McpOAuthError):
    """已通过 SDK metadata／resource／issuer 校验，尚需用户授权。"""


def is_authorization_required(error: BaseException) -> bool:
    """SDK task group 可能包装异常；清理错误不能被一起当成正常待授权。"""
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(is_authorization_required(item) for item in error.exceptions)
    return isinstance(error, McpAuthorizationRequired)


class _OAuthLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # SDK 会记录原 token 响应与 callback state 异常。此专属 logger 的所有
        # 自由正文均不进入产品日志；调用方另行报告安全的失败类型与阶段。
        error = record.exc_info[1] if record.exc_info else None
        record.msg = "MCP OAuth SDK: event=%s error_type=%s"
        record.args = (record.funcName, type(error).__name__ if error is not None else "none")
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_oauth_log_filter = _OAuthLogFilter()


class McpOAuthProvider(OAuthClientProvider):
    """不另写 OAuth 协议；授权重定向及回调由实际连接操作注入。"""

    def __init__(
        self, *, server: McpServerConfig, workspace_root: Path,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None = None,
    ) -> None:
        if server.oauth is None or server.transport != "streamable_http":
            raise ValueError("MCP OAuth 仅适用于显式配置的 HTTP Server")
        connection = cast(StreamableHttpConnection, server.connection)
        self._credentials = McpOAuthCredentialStore(
            workspace_root=workspace_root, server_id=server.server_id,
            server_url=connection["url"], redirect_uri=server.oauth.redirect_uri,
        )
        self._redirect = redirect_handler
        self._callback = callback_handler
        logger = logging.getLogger("mcp.client.auth.oauth2")
        if _oauth_log_filter not in logger.filters:
            logger.addFilter(_oauth_log_filter)
        super().__init__(
            server_url=connection["url"], storage=self._credentials,
            client_metadata=OAuthClientMetadata(
                client_name="BoxTeam", redirect_uris=[AnyUrl(server.oauth.redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"], token_endpoint_auth_method="none",
            ),
            redirect_handler=self._redirect_supported, callback_handler=self._receive_callback,
        )

    async def _initialize(self) -> None:
        # TODO: 官方 SDK 当前初始化未恢复绝对过期时间或 discovery metadata；
        # 保持该适配集中于此，升级 SDK 时核对并删除已由上游承担的部分。
        await super()._initialize()
        tokens = self.context.current_tokens
        if tokens is not None:
            self.context.update_token_expiry(tokens)
        resource, authorization = self._credentials.load_metadata()
        if resource is not None and authorization is not None:
            await self._validate_resource_match(resource)
            self.context.protected_resource_metadata = resource
            self.context.oauth_metadata = authorization
            self.context.auth_server_url = str(resource.authorization_servers[0])
            self._validate_discovery()

    def _validate_discovery(self) -> None:
        resource, authorization = self.context.protected_resource_metadata, self.context.oauth_metadata
        if resource is None or authorization is None or self.context.auth_server_url is None:
            raise McpOAuthError("unsupported-auth-method: 缺少已确认的 MCP/OAuth metadata")
        if str(authorization.issuer) != str(AnyUrl(self.context.auth_server_url)):
            raise McpOAuthError("unsupported-auth-method: OAuth issuer 与所选授权服务器不一致")
        for endpoint in (authorization.issuer, authorization.authorization_endpoint, authorization.token_endpoint,
                         authorization.registration_endpoint):
            if endpoint is None:
                continue
            parsed = urlparse(str(endpoint))
            if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
                    or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
                raise McpOAuthError("unsupported-auth-method: OAuth endpoint 不符合 HTTPS/loopback 合同")
        if "S256" not in (authorization.code_challenge_methods_supported or []):
            raise McpOAuthError("unsupported-auth-method: OAuth Server 未声明 S256 PKCE")

    async def _redirect_supported(self, url: str) -> None:
        self._validate_discovery()
        resource, authorization = self.context.protected_resource_metadata, self.context.oauth_metadata
        assert resource is not None and authorization is not None
        self._credentials.save_metadata(resource, authorization)
        if self._redirect is None:
            raise McpAuthorizationRequired("MCP Server 需要用户完成 OAuth 授权")
        await self._redirect(url)

    async def _receive_callback(self) -> tuple[str, str | None]:
        if self._callback is None:
            raise McpOAuthError("MCP OAuth 缺少实际 owner 的回调通道")
        return await self._callback()

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        flow = super().async_auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                if outgoing is not request and outgoing.method == "POST":
                    # SDK 完成 discovery 后才请求 DCR／token；副作用前确认支持合同。
                    self._validate_discovery()
                response = yield outgoing
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        except McpOAuthError:
            raise
        except Exception as error:  # noqa: BLE001 - 必须阻止第三方自由异常正文泄漏 OAuth 凭据，仍以安全类型明确失败。
            # SDK 错误可能携带 token 响应／state；原件也不能进入 owner 异常链。
            raise McpOAuthError(f"MCP OAuth 失败: error_type={type(error).__name__}") from None
        finally:
            await flow.aclose()
