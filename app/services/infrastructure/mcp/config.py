from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from langchain_mcp_adapters.sessions import Connection

from app.schemas.internal_v2.mcp import (
    McpPrivateHeaderRefDTO,
    McpServiceReferenceDTO,
    require_mcp_http_url,
)
from app.services.infrastructure.mcp.private_inputs import McpPrivateInputStore

McpTransport = Literal["plugin", "streamable_http"]
_SERVER_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
_PLACEHOLDER_PATTERN = re.compile(r"\$\{([^}]+)\}")
_COMMON_FIELDS = frozenset({"enabled", "transport"})
_PLUGIN_FIELDS = _COMMON_FIELDS | frozenset({"service"})
_HTTP_FIELDS = _COMMON_FIELDS | frozenset({"url", "headers", "oauth"})


@dataclass(frozen=True, slots=True)
class McpOAuthConfig:
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    enabled: bool
    transport: McpTransport
    connection: Connection | None
    oauth: McpOAuthConfig | None = None
    service: McpServiceReferenceDTO | None = None
    private_headers: tuple[McpPrivateHeaderRefDTO, ...] = ()


def parse_mcp_server_configs(raw_config: object, *, workspace_root: Path | None) -> tuple[McpServerConfig, ...]:
    if raw_config is None:
        return ()
    if not isinstance(raw_config, dict):
        raise TypeError("mcp 配置必须是对象")
    if set(raw_config) - {"servers", "supervision"}:
        raise ValueError("mcp 配置只接受 servers 和 supervision")
    sealed_call_timeout_seconds(raw_config)
    raw_servers = raw_config.get("servers", {})
    if not isinstance(raw_servers, dict):
        raise TypeError("mcp.servers 必须是对象")
    servers: list[McpServerConfig] = []
    for server_id, raw_server in raw_servers.items():
        if not isinstance(server_id, str) or not _SERVER_ID_PATTERN.fullmatch(server_id):
            raise ValueError(f"MCP Server ID 格式无效: {server_id!r}")
        servers.append(_parse_server(server_id, raw_server, workspace_root=workspace_root))
    return tuple(servers)


def sealed_call_timeout_seconds(raw_config: object) -> int:
    if raw_config is None:
        return 1800
    if not isinstance(raw_config, dict):
        raise TypeError("mcp 配置必须是对象")
    supervision = raw_config.get("supervision", {})
    if not isinstance(supervision, dict) or set(supervision) - {"sealed_call_timeout_seconds"}:
        raise ValueError("mcp.supervision 必须是仅包含受支持监督参数的对象")
    value = supervision.get("sealed_call_timeout_seconds", 1800)
    if type(value) is not int or not 1 <= value <= 86400:
        raise ValueError("mcp.supervision.sealed_call_timeout_seconds 必须是 1–86400 的整数")
    return value


def _parse_server(server_id: str, raw_server: object, *, workspace_root: Path | None) -> McpServerConfig:
    if not isinstance(raw_server, dict):
        raise TypeError(f"mcp.servers.{server_id} 必须是对象")
    enabled, transport = raw_server.get("enabled", False), raw_server.get("transport")
    if not isinstance(enabled, bool):
        raise TypeError(f"mcp.servers.{server_id}.enabled 必须是布尔值")
    if transport == "plugin":
        _reject_unknown_fields(server_id, raw_server, _PLUGIN_FIELDS)
        service = McpServiceReferenceDTO.model_validate(raw_server.get("service"))
        if workspace_root is None:
            raise ValueError("插件 MCP 服务必须处于显式工作区上下文")
        return McpServerConfig(server_id, enabled, transport, None, service=service)
    if transport == "streamable_http":
        _reject_unknown_fields(server_id, raw_server, _HTTP_FIELDS)
        connection, private_headers = _parse_http_connection(server_id, raw_server, workspace_root=workspace_root)
        return McpServerConfig(server_id, enabled, transport, connection,
            oauth=_parse_oauth(server_id, raw_server, workspace_root=workspace_root),
            private_headers=private_headers)
    raise ValueError(f"mcp.servers.{server_id}.transport 仅支持 plugin 或 streamable_http；Backend 不再启动 stdio")


def _parse_oauth(server_id: str, raw_server: dict[str, object], *, workspace_root: Path | None) -> McpOAuthConfig | None:
    if "oauth" not in raw_server:
        return None
    oauth = raw_server["oauth"]
    if not isinstance(oauth, dict) or set(oauth) != {"redirect_uri"}:
        raise ValueError(f"mcp.servers.{server_id}.oauth 必须仅包含 redirect_uri")
    redirect_uri = _expand_value(_required_string(oauth["redirect_uri"],
        f"mcp.servers.{server_id}.oauth.redirect_uri"), workspace_root=workspace_root)
    require_mcp_http_url(redirect_uri, field=f"mcp.servers.{server_id}.oauth.redirect_uri")
    parsed = urlparse(redirect_uri)
    if parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"mcp.servers.{server_id}.oauth.redirect_uri 不得包含凭据或 fragment")
    headers = raw_server.get("headers", {})
    if isinstance(headers, dict) and any(str(key).lower() == "authorization" for key in headers):
        raise ValueError(f"mcp.servers.{server_id} 不得同时配置 OAuth 与 Authorization header")
    return McpOAuthConfig(redirect_uri=redirect_uri)


def _parse_http_connection(server_id: str, raw_server: dict[str, object], *, workspace_root: Path | None
                            ) -> tuple[Connection, tuple[McpPrivateHeaderRefDTO, ...]]:
    url = _expand_value(_required_string(raw_server.get("url"), f"mcp.servers.{server_id}.url"),
                        workspace_root=workspace_root)
    require_mcp_http_url(url, field=f"mcp.servers.{server_id}.url")
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"mcp.servers.{server_id}.url 不得包含凭据或 fragment")
    raw_headers = raw_server.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise TypeError(f"mcp.servers.{server_id}.headers 必须是对象")
    headers: dict[str, str] = {}
    private_headers: list[McpPrivateHeaderRefDTO] = []
    for key, value in raw_headers.items():
        if not isinstance(key, str):
            raise TypeError(f"mcp.servers.{server_id}.headers 名称必须是字符串")
        if isinstance(value, str):
            headers[key] = _expand_value(value, workspace_root=workspace_root)
            continue
        reference = McpPrivateHeaderRefDTO.model_validate(value)
        if workspace_root is None:
            raise ValueError("MCP 私有 header 需要明确工作区配置上下文")
        McpPrivateInputStore(workspace_root=workspace_root).require_binding(
            reference, server_id=server_id, header_name=key, endpoint=url,
        )
        private_headers.append(reference)
    return {"transport": "streamable_http", "url": url, "headers": headers}, tuple(private_headers)


def _expand_value(value: str, *, workspace_root: Path | None) -> str:
    def resolve(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "workspaceRoot":
            if workspace_root is None:
                raise ValueError("MCP workspaceRoot 需要明确工作区配置上下文")
            return str(workspace_root)
        resolved = os.environ.get(name)
        if resolved is None:
            raise ValueError(f"MCP 配置引用了未定义的环境变量: {name}")
        return resolved
    return _PLACEHOLDER_PATTERN.sub(resolve, value)


def _reject_unknown_fields(server_id: str, raw_server: dict[str, object], supported: frozenset[str]) -> None:
    unknown = set(raw_server) - supported
    if unknown:
        raise ValueError(f"mcp.servers.{server_id} 包含不支持的字段: " + ", ".join(sorted(unknown)))


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} 必须是非空字符串")
    return value.strip()
