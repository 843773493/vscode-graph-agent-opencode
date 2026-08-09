from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.config import get_config_sources
from app.services.infrastructure.config_service import ConfigService


def _base_config() -> dict[str, object]:
    return {
        "config_version": 1,
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
async def test_config_sources_endpoint_exposes_layers_and_schema(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps(_base_config()), encoding="utf-8")
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(
        json.dumps({"logger": {"level": "debug"}}),
        encoding="utf-8",
    )
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )

    response = await get_config_sources(
        _="local-dev-token",
        request_id="req-config-sources",
        config_service=service,
    )

    assert response.request_id == "req-config-sources"
    assert response.data is not None
    assert response.data.schema_path.endswith("workspace_schema.jsonc")
    assert [source.layer for source in response.data.sources] == [
        "inline",
        "user",
        "user_local",
    ]
    assert response.data.sources[2].loaded is True
