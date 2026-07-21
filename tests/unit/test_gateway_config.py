from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from app.gateway.config import load_gateway_config, resolve_gateway_path


def _write_workspace_gateway_override(
    workspace_root: Path,
    workspace: dict[str, object],
) -> None:
    boxteam_root = workspace_root / ".boxteam"
    boxteam_root.mkdir(parents=True)
    (boxteam_root / "boxteam.json").write_text(
        json.dumps({"gateway": {"workspaces": [workspace]}}),
        encoding="utf-8",
    )


def test_load_gateway_config_rejects_legacy_direct_backend_fields(tmp_path: Path):
    _write_workspace_gateway_override(
        tmp_path,
        {
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "remote_workspace_path": "/workspace/project",
        },
    )

    with pytest.raises(
        jsonschema.ValidationError,
        match="remote_workspace_path.*unexpected",
    ):
        load_gateway_config(tmp_path)


def test_load_gateway_config_accepts_remote_gateway(tmp_path: Path):
    _write_workspace_gateway_override(
        tmp_path,
        {
            "kind": "remote_gateway",
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "remote_gateway_port": 9014,
        },
    )

    result = load_gateway_config(tmp_path)

    assert len(result.workspaces) == 1
    workspace = result.workspaces[0]
    assert workspace.kind == "remote_gateway"
    assert workspace.port == 22
    assert workspace.remote_gateway_port == 9014
    assert workspace.activate is False


def test_load_gateway_config_rejects_schema_violation(tmp_path: Path):
    _write_workspace_gateway_override(
        tmp_path,
        {
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "port": 70000,
        },
    )

    with pytest.raises(jsonschema.ValidationError):
        load_gateway_config(tmp_path)


def test_load_gateway_config_skips_disabled_workspace(tmp_path: Path):
    _write_workspace_gateway_override(
        tmp_path,
        {
            "enabled": False,
            "host": "127.0.0.1",
            "username": "root",
            "private_key_path": "~/.ssh/boxteam_gateway_e2e_ed25519",
            "remote_gateway_port": 8014,
        },
    )

    assert load_gateway_config(tmp_path).workspaces == ()


def test_resolve_gateway_relative_path_uses_installed_config_directory(tmp_path: Path):
    assert resolve_gateway_path(
        "keys/gateway_ed25519",
        config_root=tmp_path,
    ) == (tmp_path / "keys" / "gateway_ed25519").resolve()
