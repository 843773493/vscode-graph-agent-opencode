from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.node_debug_service import NodeDebugService


def _write_config(tmp_path: Path, debug: dict) -> Path:
    path = tmp_path / "workspace.jsonc"
    path.write_text(json.dumps({"runtime": {"debug": debug}}), encoding="utf-8")
    return path


def test_debug_runtime_config_uses_loopback_dynamic_defaults(tmp_path: Path) -> None:
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_config(tmp_path, {}),
    )

    config = service.get_debug_runtime_config()

    assert config["enabled"] is True
    assert config["default_adapter"] == "node_inspector"
    assert config["node"]["inspector_host"] == "127.0.0.1"
    assert config["node"]["inspector_port"] == 0
    assert config["python"]["debugpy_port"] == 0


def test_debug_runtime_config_rejects_public_inspector_host(tmp_path: Path) -> None:
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_config(
            tmp_path,
            {"node": {"inspector_host": "0.0.0.0"}},
        ),
    )

    with pytest.raises(ValueError, match="必须是 loopback 地址"):
        service.get_debug_runtime_config()


def test_debug_runtime_config_reads_launch_profile_and_timeout(tmp_path: Path) -> None:
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_config(
            tmp_path,
            {
                "command_timeout_seconds": 4,
                "launch_profiles": {
                    "node-test": {
                        "adapter": "node_inspector",
                        "runtime": "node",
                        "program": "fixtures/debug.mjs",
                        "working_directory": "fixtures",
                        "args": ["--fixture"],
                    }
                },
            },
        ),
    )

    config = service.get_debug_runtime_config()

    assert config["command_timeout_seconds"] == 4.0
    assert config["launch_profiles"]["node-test"]["program"] == (
        "fixtures/debug.mjs"
    )
    assert config["launch_profiles"]["node-test"]["args"] == ["--fixture"]


def test_node_debug_capabilities_hide_runtime_endpoints_and_mark_support(
    tmp_path: Path,
) -> None:
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_config(
            tmp_path,
            {
                "node": {
                    "inspector_host": "127.0.0.1",
                    "inspector_port": 8217,
                },
                "launch_profiles": {
                    "node-test": {
                        "adapter": "node_inspector",
                        "runtime": "node",
                        "program": "fixtures/debug.mjs",
                        "working_directory": "fixtures",
                        "args": ["--fixture"],
                    },
                    "python-future": {
                        "adapter": "debugpy",
                        "runtime": "python",
                        "program": "fixtures/debug.py",
                        "working_directory": "fixtures",
                        "args": [],
                    },
                },
            },
        ),
    )
    service = NodeDebugService(
        workspace_root=tmp_path,
        config_service=config_service,
    )

    capabilities = service.get_capabilities()
    payload = capabilities.model_dump(mode="json")

    assert payload["supported_adapters"] == ["node_inspector"]
    profiles = {item["name"]: item for item in payload["launch_profiles"]}
    assert profiles["node-test"]["supported"] is True
    assert profiles["python-future"]["supported"] is False
    assert "inspector_host" not in payload
    assert "inspector_port" not in payload
    assert "debugpy_port" not in payload


def test_debug_runtime_config_reads_workspace_override(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    override_dir = workspace_root / ".boxteam"
    override_dir.mkdir(parents=True)
    base_config = _write_config(tmp_path, {})
    (override_dir / "workspace.jsonc").write_text(
        json.dumps(
            {
                "runtime": {
                    "debug": {
                        "command_timeout_seconds": 3,
                        "node": {"inspector_port": 8217},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=base_config,
        workspace_root=workspace_root,
    )

    config = service.get_debug_runtime_config()

    assert config["command_timeout_seconds"] == 3.0
    assert config["node"]["inspector_port"] == 8217


@pytest.mark.parametrize(
    ("debug", "message"),
    [
        ({"node": {"inspector_port": 65536}}, "inspector_port"),
        ({"command_timeout_seconds": 0}, "command_timeout_seconds"),
    ],
)
def test_debug_runtime_config_rejects_invalid_operational_values(
    tmp_path: Path,
    debug: dict,
    message: str,
) -> None:
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_config(tmp_path, debug),
    )

    with pytest.raises(ValueError, match=message):
        service.get_debug_runtime_config()


def test_debug_runtime_schema_rejects_unknown_fields(tmp_path: Path) -> None:
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_config(tmp_path, {"unexpected": True}),
    )

    with pytest.raises(jsonschema.ValidationError, match="unexpected"):
        service.validate_workspace_config()


def test_debug_tools_use_existing_confirmation_and_denylist_policy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.jsonc"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "default": {
                        "tools": {
                            "denylist": ["evaluate_expression"],
                            "confirmation_required": ["evaluate_expression"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=path,
    )

    policy = service.resolve_agent_tool_policy("default")
    confirmations = service.resolve_agent_confirmation_tool_names("default")

    assert "evaluate_expression" in policy.disabled_names
    assert confirmations == frozenset({"evaluate_expression"})
