from __future__ import annotations

import json
from pathlib import Path

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
                    "interface": "opencode_zen",
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
