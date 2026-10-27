from __future__ import annotations

from pathlib import Path

import pytest

from app.services.infrastructure.mcp.config import (
    parse_mcp_server_configs,
    sealed_call_timeout_seconds,
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def test_oauth_callback_is_explicit_and_target_owned(workspace_root, monkeypatch):
    monkeypatch.setenv("MCP_CALLBACK", "https://target.example/api/v1/mcp/oauth/callback")
    raw = {"transport": "streamable_http", "url": "https://mcp.example/api", "oauth": {"redirect_uri": "${MCP_CALLBACK}"}}
    server, = parse_mcp_server_configs({"servers": {"secure": raw}}, workspace_root=workspace_root)
    assert server.oauth is not None
    assert server.oauth.redirect_uri == "https://target.example/api/v1/mcp/oauth/callback"
    assert "oauth" not in server.connection


@pytest.mark.parametrize("oauth", [None, {}, {"unknown": "value"}, {"redirect_uri": "http://remote.example/callback"},
                                   {"redirect_uri": "file:///tmp/callback"}, {"redirect_uri": "https://host/callback#secret"},
                                   {"redirect_uri": "https://user:secret@host/callback"}])
def test_oauth_configuration_rejects_unsupported_callback_inputs(workspace_root, oauth):
    raw = {"transport": "streamable_http", "url": "https://mcp.example/api", "oauth": oauth}
    with pytest.raises(ValueError):
        parse_mcp_server_configs({"servers": {"secure": raw}}, workspace_root=workspace_root)


def test_oauth_cannot_override_an_explicit_authorization_header(workspace_root):
    raw = {"transport": "streamable_http", "url": "https://mcp.example/api", "headers": {"aUtHoRiZaTiOn": "Bearer existing"},
           "oauth": {"redirect_uri": "https://target.example/callback"}}
    with pytest.raises(ValueError, match="同时配置"):
        parse_mcp_server_configs({"servers": {"secure": raw}}, workspace_root=workspace_root)


def test_stdio_does_not_accept_http_oauth_configuration(workspace_root):
    with pytest.raises(ValueError, match="不支持的字段"):
        parse_mcp_server_configs({"servers": {"secure": {"transport": "stdio", "command": "server", "oauth": {}}}},
                                 workspace_root=workspace_root)


@pytest.mark.parametrize("seconds", [1, 1800, 86400])
def test_sealed_call_timeout_uses_one_bounded_configuration(workspace_root, seconds):
    config = {"supervision": {"sealed_call_timeout_seconds": seconds}, "servers": {}}
    assert parse_mcp_server_configs(config, workspace_root=workspace_root) == ()
    assert sealed_call_timeout_seconds(config) == seconds
    assert sealed_call_timeout_seconds({}) == sealed_call_timeout_seconds(None) == 1800


@pytest.mark.parametrize("value", [0, 86401, -1, True, 1.5, "1800", None])
def test_sealed_call_timeout_rejects_invalid_and_unbounded_values(workspace_root, value):
    with pytest.raises(ValueError, match="1–86400"):
        parse_mcp_server_configs({"supervision": {"sealed_call_timeout_seconds": value}}, workspace_root=workspace_root)


def test_sealed_call_timeout_rejects_unknown_supervision_fields(workspace_root):
    with pytest.raises(ValueError, match="受支持监督参数"):
        parse_mcp_server_configs({"supervision": {"timeout": 1}}, workspace_root=workspace_root)


def test_parse_stdio_config_expands_workspace_and_environment(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "secret-value")

    servers = parse_mcp_server_configs(
        {
            "servers": {
                "mini": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "${workspaceRoot}/bin/server",
                    "args": ["--root", "${workspaceRoot}"],
                    "env": {"TOKEN": "${MCP_TEST_TOKEN}"},
                }
            }
        },
        workspace_root=workspace_root,
    )

    assert len(servers) == 1
    server = servers[0]
    assert server.enabled is True
    connection = server.connection
    assert connection["transport"] == "stdio"
    assert connection["command"] == f"{workspace_root}/bin/server"
    assert connection["args"] == ["--root", str(workspace_root)]
    assert connection.get("env") == {"TOKEN": "secret-value"}
    assert str(connection.get("cwd")) == str(workspace_root)


def test_parse_config_rejects_missing_environment_variable(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_MISSING_TOKEN", raising=False)

    with pytest.raises(
        ValueError,
        match="未定义的环境变量: MCP_MISSING_TOKEN",
    ):
        parse_mcp_server_configs(
            {
                "servers": {
                    "remote": {
                        "enabled": True,
                        "transport": "streamable_http",
                        "url": "https://example.com/mcp",
                        "headers": {
                            "Authorization": "Bearer ${MCP_MISSING_TOKEN}"
                        },
                    }
                }
            },
            workspace_root=workspace_root,
        )


def test_parse_config_rejects_non_loopback_plain_http(
    workspace_root: Path,
) -> None:
    with pytest.raises(ValueError, match="仅允许 loopback 地址使用 HTTP"):
        parse_mcp_server_configs(
            {
                "servers": {
                    "remote": {
                        "transport": "streamable_http",
                        "url": "http://example.com/mcp",
                    }
                }
            },
            workspace_root=workspace_root,
        )
