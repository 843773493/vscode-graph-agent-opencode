from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from app.gateway.config import load_gateway_config


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


def test_load_gateway_config_uses_json_schema_and_applies_defaults(tmp_path: Path):
    _write_workspace_gateway_override(
        tmp_path,
        {
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "remote_workspace_path": "/workspace/project",
        },
    )

    result = load_gateway_config(tmp_path)

    assert len(result.workspaces) == 1
    workspace = result.workspaces[0]
    assert workspace.kind == "ssh"
    assert workspace.port == 22
    assert workspace.remote_backend_host == "127.0.0.1"
    assert workspace.remote_backend_port == 8010
    assert workspace.activate is False


def test_load_gateway_config_rejects_schema_violation(tmp_path: Path):
    _write_workspace_gateway_override(
        tmp_path,
        {
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "remote_workspace_path": "/workspace/project",
            "port": 70000,
        },
    )

    with pytest.raises(jsonschema.ValidationError):
        load_gateway_config(tmp_path)
