from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from langchain_mcp_adapters.sessions import Connection

from app.schemas.internal_v2.mcp import (
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
    McpPrivateEnvRefDTO,
    McpPrivateHeaderRefDTO,
    McpProviderDefinitionRefDTO,
    require_mcp_http_url,
)
from app.services.infrastructure.mcp.private_inputs import (
    McpPrivateInputStore,
    candidate_env_definition,
    stdio_env_definition,
)

McpTransport = Literal["stdio", "streamable_http"]
_SERVER_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
_PLACEHOLDER_PATTERN = re.compile(r"\$\{([^}]+)\}")
_COMMON_FIELDS = frozenset({"enabled", "transport"})
_STDIO_FIELDS = _COMMON_FIELDS | frozenset({"command", "managed_launch", "args", "env", "cwd"})
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
    managed_launch: McpManagedLaunchDTO | None = None
    private_headers: tuple[McpPrivateHeaderRefDTO, ...] = ()
    private_env: tuple[McpPrivateEnvRefDTO, ...] = ()
    provider_definition: McpProviderDefinitionRefDTO | None = None


def parse_mcp_server_configs(
    raw_config: object,
    *,
    workspace_root: Path | None,
    installation_resolver: Callable[[McpManagedLaunchDTO], McpInstalledArtifactDTO] | None = None,
) -> tuple[McpServerConfig, ...]:
    if raw_config is None:
        return ()
    if not isinstance(raw_config, dict):
        raise TypeError("mcp 配置必须是对象")
    unknown_root_fields = set(raw_config) - {"servers", "supervision"}
    if unknown_root_fields:
        raise ValueError(
            "mcp 配置包含不支持的字段: "
            + ", ".join(sorted(unknown_root_fields))
        )
    sealed_call_timeout_seconds(raw_config)
    raw_servers = raw_config.get("servers", {})
    if not isinstance(raw_servers, dict):
        raise TypeError("mcp.servers 必须是对象")

    server_configs: list[McpServerConfig] = []
    for server_id, raw_server in raw_servers.items():
        if not isinstance(server_id, str) or not _SERVER_ID_PATTERN.fullmatch(server_id):
            raise ValueError(f"MCP Server ID 格式无效: {server_id!r}")
        server_configs.append(
            _parse_server(
                server_id,
                raw_server,
                workspace_root=workspace_root,
                installation_resolver=installation_resolver,
            )
        )
    return tuple(server_configs)


def sealed_call_timeout_seconds(raw_config: object) -> int:
    """同一 MCP 配置族的有界封存资格；更改仍沿现有重启策略生效。"""
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


def _parse_server(
    server_id: str,
    raw_server: object,
    *,
    workspace_root: Path | None,
    installation_resolver: Callable[[McpManagedLaunchDTO], McpInstalledArtifactDTO] | None,
) -> McpServerConfig:
    if not isinstance(raw_server, dict):
        raise TypeError(f"mcp.servers.{server_id} 必须是对象")
    enabled = raw_server.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TypeError(f"mcp.servers.{server_id}.enabled 必须是布尔值")
    transport = raw_server.get("transport")
    if "provider_definition" in raw_server:
        _reject_unknown_fields(server_id, raw_server, _COMMON_FIELDS | {"provider_definition"})
        if transport not in {"stdio", "streamable_http"}:
            raise ValueError("动态 MCP 定义必须声明真实的 stdio 或 streamable_http 传输")
        reference = McpProviderDefinitionRefDTO.model_validate(raw_server["provider_definition"])
        if reference.host_kind == "web" and transport == "stdio":
            raise ValueError("Web Provider 没有目标原生进程上下文，不能提供 stdio 启动定义")
        return McpServerConfig(server_id=server_id, enabled=enabled, transport=transport,
            connection=None, provider_definition=reference)
    if transport == "stdio":
        binding = McpManagedLaunchDTO.model_validate(raw_server["managed_launch"]) if "managed_launch" in raw_server else None
        if binding is not None and ("command" in raw_server or installation_resolver is None):
            raise ValueError(f"mcp.servers.{server_id} 受管 launch 必须由目标安装 owner 解析，且不能另设 command")
        artifact = installation_resolver(binding) if binding is not None and installation_resolver is not None else None
        connection, private_env = _parse_stdio_connection(server_id, raw_server, workspace_root=workspace_root,
            managed_artifact=artifact)
        return McpServerConfig(
            server_id=server_id,
            enabled=enabled,
            transport=transport,
            connection=connection,
            managed_launch=binding,
            private_env=private_env,
        )
    if transport == "streamable_http":
        connection, private_headers = _parse_http_connection(server_id, raw_server, workspace_root=workspace_root)
        return McpServerConfig(
            server_id=server_id,
            enabled=enabled,
            transport=transport,
            connection=connection,
            oauth=_parse_oauth(server_id, raw_server, workspace_root=workspace_root),
            private_headers=private_headers,
        )
    raise ValueError(
        f"mcp.servers.{server_id}.transport 仅支持 stdio 或 streamable_http"
    )


def _parse_oauth(
    server_id: str, raw_server: dict[str, object], *, workspace_root: Path | None,
) -> McpOAuthConfig | None:
    if "oauth" not in raw_server:
        return None
    oauth = raw_server["oauth"]
    if not isinstance(oauth, dict) or set(oauth) != {"redirect_uri"}:
        raise ValueError(f"mcp.servers.{server_id}.oauth 必须仅包含 redirect_uri")
    redirect_uri = _expand_value(
        _required_string(oauth["redirect_uri"], f"mcp.servers.{server_id}.oauth.redirect_uri"),
        workspace_root=workspace_root,
    )
    require_mcp_http_url(redirect_uri, field=f"mcp.servers.{server_id}.oauth.redirect_uri")
    parsed = urlparse(redirect_uri)
    if parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"mcp.servers.{server_id}.oauth.redirect_uri 不得包含凭据或 fragment")
    headers = cast(dict[str, object], raw_server.get("headers", {}))
    if any(str(key).lower() == "authorization" for key in headers):
        raise ValueError(f"mcp.servers.{server_id} 不得同时配置 OAuth 与 Authorization header")
    return McpOAuthConfig(redirect_uri=redirect_uri)


def _parse_stdio_connection(
    server_id: str,
    raw_server: dict[str, object],
    *,
    workspace_root: Path | None,
    managed_artifact: McpInstalledArtifactDTO | None = None,
) -> tuple[Connection, tuple[McpPrivateEnvRefDTO, ...]]:
    _reject_unknown_fields(server_id, raw_server, _STDIO_FIELDS)
    managed_command = managed_artifact.command if managed_artifact is not None else None
    command = managed_command or _required_string(raw_server.get("command"), f"mcp.servers.{server_id}.command")
    args = _string_list(raw_server.get("args", []), f"mcp.servers.{server_id}.args")
    raw_env = raw_server.get("env", {})
    if not isinstance(raw_env, dict) or not all(isinstance(key, str) for key in raw_env):
        raise TypeError(f"mcp.servers.{server_id}.env 必须是字符串键的对象")
    if any(not isinstance(value, (str, dict)) for value in raw_env.values()):
        raise TypeError(f"mcp.servers.{server_id}.env 只接受字符串或私有引用对象")
    private_env = {key: McpPrivateEnvRefDTO.model_validate(value) for key, value in raw_env.items() if not isinstance(value, str)}
    plain_env = {key: value for key, value in raw_env.items() if isinstance(value, str)}
    if managed_artifact is not None and managed_artifact.registry_type == "pypi":
        if any(key.upper() in {"PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"} for key in raw_env):
            raise ValueError("受管 MCP 制品不能通过 Python 导入路径环境变量替换锁定依赖")
        plain_env = {**plain_env, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        if any(key.upper() in {"PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"} and not isinstance(value, str)
               for key, value in raw_env.items()):
            raise ValueError("受管 MCP 制品的 Python 隔离参数不能使用私有输入替换")
    if (managed_artifact is not None and managed_artifact.registry_type == "npm"
            and any(key.upper() in {"NODE_PATH", "NODE_OPTIONS"} for key in raw_env)):
        raise ValueError("受管 npm 制品不能通过 Node 导入路径或启动选项替换锁定依赖")

    raw_cwd = raw_server.get("cwd")
    cwd = (
        (str(workspace_root) if workspace_root is not None else None)
        if raw_cwd is None
        else _expand_value(
            _required_string(raw_cwd, f"mcp.servers.{server_id}.cwd"),
            workspace_root=workspace_root,
        )
    )
    connection: Connection = {
        "transport": "stdio",
        "command": managed_command if managed_command is not None else _expand_value(command, workspace_root=workspace_root),
        "args": [
            _expand_value(argument, workspace_root=workspace_root)
            for argument in args
        ],
        "env": {
            key: _expand_value(value, workspace_root=workspace_root)
            for key, value in plain_env.items()
        },
        "cwd": cwd,
    }
    if private_env:
        definition = (candidate_env_definition(managed_artifact.source, managed_artifact.manifest_digest, managed_artifact.variant)
                      if managed_artifact is not None else stdio_env_definition(connection))
        if workspace_root is None:
            raise ValueError("MCP 私有 env 需要明确工作区配置上下文")
        private_store = McpPrivateInputStore(workspace_root=workspace_root)
        for key, reference in private_env.items():
            private_store.require_env_binding(reference, server_id=server_id,
                input_key=key, definition_digest=definition)
    return connection, tuple(private_env.values())


def _parse_http_connection(
    server_id: str,
    raw_server: dict[str, object],
    *,
    workspace_root: Path | None,
) -> tuple[Connection, tuple[McpPrivateHeaderRefDTO, ...]]:
    _reject_unknown_fields(server_id, raw_server, _HTTP_FIELDS)
    url = _expand_value(
        _required_string(raw_server.get("url"), f"mcp.servers.{server_id}.url"),
        workspace_root=workspace_root,
    )
    require_mcp_http_url(url, field=f"mcp.servers.{server_id}.url")
    raw_headers = raw_server.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise TypeError(f"mcp.servers.{server_id}.headers 必须是对象")
    headers: dict[str, str] = {}
    private_headers = []
    for key, value in raw_headers.items():
        if not isinstance(key, str):
            raise TypeError(f"mcp.servers.{server_id}.headers 名称必须是字符串")
        if isinstance(value, str):
            headers[key] = _expand_value(value, workspace_root=workspace_root)
        else:
            reference = McpPrivateHeaderRefDTO.model_validate(value)
            if workspace_root is None:
                raise ValueError("MCP 私有 header 需要明确工作区配置上下文")
            McpPrivateInputStore(workspace_root=workspace_root).require_binding(
                reference, server_id=server_id, header_name=key, endpoint=url,
            )
            private_headers.append(reference)
    return {
        "transport": "streamable_http",
        "url": url,
        "headers": headers,
    }, tuple(private_headers)


def _expand_value(value: str, *, workspace_root: Path | None) -> str:
    if "${" not in value:
        return value

    def resolve_placeholder(placeholder_match: re.Match[str]) -> str:
        placeholder_name = placeholder_match.group(1)
        if placeholder_name == "workspaceRoot":
            if workspace_root is None:
                raise ValueError("MCP workspaceRoot 需要明确工作区配置上下文")
            return str(workspace_root)
        environment_value = os.environ.get(placeholder_name)
        if environment_value is None:
            raise ValueError(f"MCP 配置引用了未定义的环境变量: {placeholder_name}")
        return environment_value

    return _PLACEHOLDER_PATTERN.sub(resolve_placeholder, value)


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
