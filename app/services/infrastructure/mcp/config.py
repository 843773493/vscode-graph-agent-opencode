from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from langchain_mcp_adapters.sessions import Connection


McpTransport = Literal["stdio", "streamable_http"]
_SERVER_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
_PLACEHOLDER_PATTERN = re.compile(r"\$\{([^}]+)\}")
_COMMON_FIELDS = frozenset({"enabled", "transport"})
_STDIO_FIELDS = _COMMON_FIELDS | frozenset({"command", "args", "env", "cwd"})
_HTTP_FIELDS = _COMMON_FIELDS | frozenset({"url", "headers"})


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    enabled: bool
    transport: McpTransport
    connection: Connection


def parse_mcp_server_configs(
    raw_config: object,
    *,
    workspace_root: Path,
) -> tuple[McpServerConfig, ...]:
    if raw_config is None:
        return ()
    if not isinstance(raw_config, dict):
        raise TypeError("mcp 配置必须是对象")
    unknown_root_fields = set(raw_config) - {"servers"}
    if unknown_root_fields:
        raise ValueError(
            "mcp 配置包含不支持的字段: "
            + ", ".join(sorted(unknown_root_fields))
        )
    raw_servers = raw_config.get("servers", {})
    if not isinstance(raw_servers, dict):
        raise TypeError("mcp.servers 必须是对象")

    parsed: list[McpServerConfig] = []
    for server_id, raw_server in raw_servers.items():
        if not isinstance(server_id, str) or not _SERVER_ID_PATTERN.fullmatch(server_id):
            raise ValueError(f"MCP Server ID 格式无效: {server_id!r}")
        parsed.append(
            _parse_server(
                server_id,
                raw_server,
                workspace_root=workspace_root,
            )
        )
    return tuple(parsed)


def _parse_server(
    server_id: str,
    raw_server: object,
    *,
    workspace_root: Path,
) -> McpServerConfig:
    if not isinstance(raw_server, dict):
        raise TypeError(f"mcp.servers.{server_id} 必须是对象")
    enabled = raw_server.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TypeError(f"mcp.servers.{server_id}.enabled 必须是布尔值")
    transport = raw_server.get("transport")
    if transport == "stdio":
        return McpServerConfig(
            server_id=server_id,
            enabled=enabled,
            transport=transport,
            connection=_parse_stdio_connection(
                server_id,
                raw_server,
                workspace_root=workspace_root,
            ),
        )
    if transport == "streamable_http":
        return McpServerConfig(
            server_id=server_id,
            enabled=enabled,
            transport=transport,
            connection=_parse_http_connection(
                server_id,
                raw_server,
                workspace_root=workspace_root,
            ),
        )
    raise ValueError(
        f"mcp.servers.{server_id}.transport 仅支持 stdio 或 streamable_http"
    )


def _parse_stdio_connection(
    server_id: str,
    raw_server: dict[str, object],
    *,
    workspace_root: Path,
) -> Connection:
    _reject_unknown_fields(server_id, raw_server, _STDIO_FIELDS)
    command = _required_string(raw_server.get("command"), f"mcp.servers.{server_id}.command")
    args = _string_list(raw_server.get("args", []), f"mcp.servers.{server_id}.args")
    raw_env = raw_server.get("env", {})
    if not isinstance(raw_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_env.items()
    ):
        raise TypeError(f"mcp.servers.{server_id}.env 必须是字符串到字符串的对象")

    raw_cwd = raw_server.get("cwd")
    cwd = (
        str(workspace_root)
        if raw_cwd is None
        else _expand_value(
            _required_string(raw_cwd, f"mcp.servers.{server_id}.cwd"),
            workspace_root=workspace_root,
        )
    )
    return {
        "transport": "stdio",
        "command": _expand_value(command, workspace_root=workspace_root),
        "args": [
            _expand_value(argument, workspace_root=workspace_root)
            for argument in args
        ],
        "env": {
            key: _expand_value(value, workspace_root=workspace_root)
            for key, value in raw_env.items()
        },
        "cwd": cwd,
    }


def _parse_http_connection(
    server_id: str,
    raw_server: dict[str, object],
    *,
    workspace_root: Path,
) -> Connection:
    _reject_unknown_fields(server_id, raw_server, _HTTP_FIELDS)
    url = _expand_value(
        _required_string(raw_server.get("url"), f"mcp.servers.{server_id}.url"),
        workspace_root=workspace_root,
    )
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError(f"mcp.servers.{server_id}.url 必须是有效 HTTP(S) URL")
    if parsed_url.scheme == "http" and parsed_url.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError(
            f"mcp.servers.{server_id}.url 仅允许 loopback 地址使用 HTTP"
        )
    raw_headers = raw_server.get("headers", {})
    if not isinstance(raw_headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_headers.items()
    ):
        raise TypeError(
            f"mcp.servers.{server_id}.headers 必须是字符串到字符串的对象"
        )
    return {
        "transport": "streamable_http",
        "url": url,
        "headers": {
            key: _expand_value(value, workspace_root=workspace_root)
            for key, value in raw_headers.items()
        },
    }


def _expand_value(value: str, *, workspace_root: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        variable_name = match.group(1)
        if variable_name == "workspaceRoot":
            return str(workspace_root)
        resolved = os.environ.get(variable_name)
        if resolved is None:
            raise ValueError(f"MCP 配置引用了未定义的环境变量: {variable_name}")
        return resolved

    return _PLACEHOLDER_PATTERN.sub(replace, value)


def _reject_unknown_fields(
    server_id: str,
    raw_server: dict[str, object],
    supported_fields: frozenset[str],
) -> None:
    unknown_fields = set(raw_server) - supported_fields
    if unknown_fields:
        raise ValueError(
            f"mcp.servers.{server_id} 包含不支持的字段: "
            + ", ".join(sorted(unknown_fields))
        )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} 必须是非空字符串")
    return value.strip()


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field} 必须是字符串数组")
    return list(value)
