from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.gateway.config import load_gateway_config, resolve_gateway_path
from app.gateway.main import gateway_config_sources


def _write_gateway_config(
    config_root: Path,
    workspaces: list[dict[str, object]],
) -> tuple[Path, Path]:
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / "gateway.jsonc"
    schema_path = config_root / "gateway_schema.jsonc"
    config_path.write_text(
        json.dumps(
            {
                "$schema": "./gateway_schema.jsonc",
                "config_version": 1,
                "workspaces": workspaces,
            }
        ),
        encoding="utf-8",
    )
    schema_path.write_bytes(Path("configs/gateway_schema.jsonc").read_bytes())
    return config_path, schema_path


def test_load_gateway_config_accepts_remote_gateway(tmp_path: Path) -> None:
    config_path, schema_path = _write_gateway_config(
        tmp_path,
        [
            {
                "kind": "remote_gateway",
                "host": "remote.example.com",
                "username": "developer",
                "private_key_path": "keys/id_ed25519",
                "ssh_config_host": "developer-server",
                "remote_pair_command": "boxteam gateway issue-federation-token",
                "remote_gateway_port": 9014,
            }
        ],
    )

    result = load_gateway_config(config_path=config_path, schema_path=schema_path)

    assert len(result.workspaces) == 1
    workspace = result.workspaces[0]
    assert workspace.port == 22
    assert workspace.ssh_config_host == "developer-server"
    assert workspace.remote_pair_command == "boxteam gateway issue-federation-token"
    assert workspace.remote_gateway_port == 9014
    assert workspace.activate is False
    assert result.default_workspace_skill_groups == (
        "browser-control",
        "gateway-context",
        "web-search-fetch",
    )


@pytest.mark.parametrize(
    "workspace",
    [
        {
            "kind": "remote_gateway",
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "remote_workspace_path": "/workspace/project",
        },
        {
            "kind": "remote_gateway",
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "port": 70000,
        },
    ],
)
def test_load_gateway_config_rejects_schema_violation(
    tmp_path: Path,
    workspace: dict[str, object],
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [workspace])

    with pytest.raises(ValueError, match="配置验证失败"):
        load_gateway_config(config_path=config_path, schema_path=schema_path)


def test_load_gateway_config_skips_disabled_workspace(tmp_path: Path) -> None:
    config_path, schema_path = _write_gateway_config(
        tmp_path,
        [
            {
                "enabled": False,
                "kind": "remote_gateway",
                "host": "127.0.0.1",
                "username": "boxteam",
                "private_key_path": "~/.ssh/boxteam_gateway_e2e_ed25519",
            }
        ],
    )

    assert (
        load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
        ).workspaces
        == ()
    )


def test_load_gateway_config_merges_local_override_and_records_sources(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    local_config_path = tmp_path / "gateway_local.jsonc"
    local_config_path.write_text(
        json.dumps(
            {
                "ui": {"theme": {"default_theme_id": "green"}},
            }
        ),
        encoding="utf-8",
    )

    result = load_gateway_config(
        config_path=config_path,
        schema_path=schema_path,
    )

    assert result.default_theme_id == "green"
    assert result.source_paths == (
        Path("configs/gateway_inline.jsonc").resolve(),
        config_path,
        local_config_path,
    )
    assert [source.layer for source in result.source_details] == [
        "inline",
        "user",
        "user_local",
    ]
    assert result.source_details[-1].loaded is True


@pytest.mark.asyncio
async def test_gateway_config_sources_endpoint_exposes_effective_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    local_config_path = tmp_path / "gateway_local.jsonc"
    local_config_path.write_text(
        json.dumps({"ui": {"theme": {"default_theme_id": "blue"}}}),
        encoding="utf-8",
    )
    config = load_gateway_config(config_path=config_path, schema_path=schema_path)
    monkeypatch.setattr("app.gateway.main.load_gateway_config", lambda: config)

    response = await gateway_config_sources(
        _="gateway-token",
        request_id="req-gateway-config-sources",
    )

    assert response.request_id == "req-gateway-config-sources"
    assert response.data is not None
    assert response.data.schema_path == str(schema_path)
    assert [source.layer for source in response.data.sources] == [
        "inline",
        "user",
        "user_local",
    ]
    assert response.data.sources[1].loaded is True


def test_gateway_loader_does_not_read_workspace_configuration(tmp_path: Path) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path / "home", [])
    workspace_config = tmp_path / "workspace" / ".boxteam" / "workspace.jsonc"
    workspace_config.parent.mkdir(parents=True)
    workspace_config.write_text(
        '{"gateway": {"workspaces": "invalid"}}\n',
        encoding="utf-8",
    )

    assert (
        load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
        ).workspaces
        == ()
    )


def test_resolve_gateway_relative_path_uses_installed_config_directory(
    tmp_path: Path,
) -> None:
    assert (
        resolve_gateway_path(
            "keys/gateway_ed25519",
            config_root=tmp_path,
        )
        == (tmp_path / "keys" / "gateway_ed25519").resolve()
    )
