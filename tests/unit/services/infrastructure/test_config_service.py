from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from app.schemas.public_v2.config import ConfigUpdateRequest
from app.services.infrastructure.config_service import ConfigService


def _write_boxteam_config(tmp_path: Path, config: dict) -> Path:
    config_path = tmp_path / "boxteam.jsonc"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _base_config() -> dict:
    return {
        "llm": {
            "providers": [
                {
                    "id": "primary",
                    "endpoint": "https://example.com/v1",
                    "model": "model-a",
                    "api_key": "${TEST_API_KEY}",
                    "custom_llm_provider": "openai",
                }
            ]
        },
        "logger": {"level": "info"},
        "default_agent": "default",
        "agents": {
            "default": {
                "name": "Default Agent",
                "instructions": {"system_prompt": "hello"},
                "model": {"primary_provider": "primary"},
            }
        },
    }


@pytest.mark.asyncio
async def test_get_public_config_resolves_default_model_from_agent_provider(tmp_path: Path):
    config_path = _write_boxteam_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    result = await service.get()

    assert result.default_model == "model-a"
    assert result.default_orchestration == "single_agent"
    assert result.max_concurrent_agents == 4
    assert result.metadata["default_agent_id"] == "default"
    assert result.metadata["config_path"] == str(config_path)


@pytest.mark.asyncio
async def test_update_public_config_uses_runtime_overrides(tmp_path: Path):
    config = _base_config()
    config["ui"] = {
        "default_orchestration": "planner",
        "max_concurrent_agents": 2,
        "allow_shell_tools": False,
        "ignored_paths": ["node_modules"],
        "auto_summarize": False,
    }
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    result = await service.update(
        ConfigUpdateRequest(
            default_model="runtime-model",
            allow_shell_tools=True,
            ignored_paths=["dist"],
        )
    )

    assert result.default_model == "runtime-model"
    assert result.default_orchestration == "planner"
    assert result.max_concurrent_agents == 2
    assert result.allow_shell_tools is True
    assert result.ignored_paths == ["dist"]
    assert result.auto_summarize is False
    assert result.metadata["runtime_overrides"] == [
        "allow_shell_tools",
        "default_model",
        "ignored_paths",
    ]


@pytest.mark.asyncio
async def test_update_public_config_null_clears_runtime_override(tmp_path: Path):
    config_path = _write_boxteam_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    await service.update(ConfigUpdateRequest(default_model="runtime-model"))
    result = await service.update(ConfigUpdateRequest(default_model=None))

    assert result.default_model == "model-a"
    assert result.metadata["runtime_overrides"] == []


@pytest.mark.asyncio
async def test_get_public_config_fails_when_default_agent_provider_is_missing(tmp_path: Path):
    config = _base_config()
    config["agents"]["default"]["model"]["primary_provider"] = "missing"
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(ValueError, match="default agent 引用了不存在的 provider: missing"):
        await service.get()


def test_get_agent_tool_config_reads_custom_tools(tmp_path: Path):
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "denylist": [],
        "confirmation_required": [],
        "custom": [
            {
                "name": "test_tool_2",
                "factory": "app.agents.tools.testing:create_test_tool_2",
            }
        ],
    }
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    result = service.get_agent_tool_config("default")

    assert result["custom"] == [
        {
            "name": "test_tool_2",
            "factory": "app.agents.tools.testing:create_test_tool_2",
        }
    ]


def test_custom_tool_options_schema_accepts_embedding_config(tmp_path: Path) -> None:
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "custom": [
            {
                "name": "fetch_webpage",
                "factory": "app.agents.tools.web:create_fetch_webpage_tool",
                "options": {
                    "embedding": {
                        "provider_id": "primary",
                        "model": "text-embedding-3-small",
                    }
                },
            }
        ]
    }
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_boxteam_config()


def test_get_llm_provider_only_resolves_selected_provider_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    config["llm"]["providers"].append(
        {
            "id": "embedding",
            "endpoint": "https://embedding.example.com/v1",
            "model": "embedding-model",
            "api_key": "${EMBEDDING_API_KEY}",
            "custom_llm_provider": "openai",
        }
    )
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-secret")
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    provider = service.get_llm_provider("embedding")

    assert provider["api_key"] == "embedding-secret"


def test_provider_request_override_schema_accepts_raw_completion_parameters(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["llm"]["providers"][0]["request_options"] = {
        "overrides": {
            "temperature": 1,
            "max_tokens": None,
            "extra_body": {
                "reasoning": True,
                "max_output_tokens": 1200,
            },
        }
    }
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_boxteam_config()


def test_agent_runtime_omits_unspecified_generation_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_boxteam_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    runtime = service.get_agent_runtime_config("default")

    assert "temperature" not in runtime
    assert "top_p" not in runtime
    assert "max_output_tokens" not in runtime


def test_provider_request_override_schema_rejects_legacy_extra_body(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["llm"]["providers"][0]["request_options"] = {
        "extra_body": {"reasoning": True}
    }
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(jsonschema.ValidationError):
        service.validate_boxteam_config()


def test_gateway_config_schema_accepts_ssh_workspace(tmp_path: Path):
    config = _base_config()
    config["gateway"] = {
        "workspaces": [
            {
                "kind": "ssh",
                "name": "remote workspace",
                "host": "127.0.0.1",
                "port": 22222,
                "username": "root",
                "private_key_path": "asset/gateway_ssh/id_ed25519",
                "remote_backend_host": "127.0.0.1",
                "remote_backend_port": 8010,
                "remote_workspace_path": "/workspace/project",
                "activate": False,
            }
        ]
    }
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_boxteam_config()


@pytest.mark.parametrize(
    ("invalid_fields", "expected_path"),
    [
        ({"remote_backend_port": 0}, ["gateway", "workspaces", 0, "remote_backend_port"]),
        ({"unexpected": True}, ["gateway", "workspaces", 0]),
    ],
)
def test_gateway_config_schema_rejects_invalid_workspace(
    tmp_path: Path,
    invalid_fields: dict[str, object],
    expected_path: list[str | int],
):
    config = _base_config()
    workspace = {
        "host": "127.0.0.1",
        "username": "root",
        "private_key_path": "id_ed25519",
        "remote_workspace_path": "/workspace/project",
        **invalid_fields,
    }
    config["gateway"] = {"workspaces": [workspace]}
    config_path = _write_boxteam_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(jsonschema.ValidationError) as error_info:
        service.validate_boxteam_config()

    assert list(error_info.value.absolute_path) == expected_path
