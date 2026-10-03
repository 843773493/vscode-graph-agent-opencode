from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from app.api.config import get_config, get_config_sources
from app.core.config_sources import ConfigSourceLayer
from app.schemas.gateway import GatewayConfigSourceDTO
from app.schemas.internal_v2.config import ConfigSourceDTO, ConfigSourcesDTO
from app.services.infrastructure.config_service import ConfigService


def test_config_source_dtos_share_the_closed_logical_layer_set() -> None:
    expected_layers = (
        "inline",
        "user",
        "user_local",
        "workspace",
        "runtime_override",
    )
    assert get_args(ConfigSourceLayer) == expected_layers

    for source_model in (ConfigSourceDTO, GatewayConfigSourceDTO):
        source = source_model(
            vrn=None,
            layer="runtime_override",
            precedence=4,
            loaded=True,
        )
        assert source.vrn is None
        assert "path" not in source.model_dump()
        assert source.layer == "runtime_override"
        with pytest.raises(ValidationError):
            source_model(vrn=None, layer="sqlite", precedence=4, loaded=True)

    sources = ConfigSourcesDTO(revision="rev-1", schema_vrn=None)
    assert sources.schema_vrn is None
    assert "schema_path" not in sources.model_dump()


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
    assert response.data.schema_vrn.startswith("boxteam://inline/")
    assert response.data.schema_vrn.endswith("/resources/config/workspace_schema")
    assert [source.layer for source in response.data.sources] == [
        "inline",
        "user",
        "user_local",
    ]
    assert response.data.sources[2].loaded is True


@pytest.mark.asyncio
async def test_config_sources_endpoint_never_exposes_real_path(
    tmp_path: Path,
) -> None:
    """响应体只含 nullable VRN locator 与来源兄弟字段，不泄漏真实路径。"""

    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps(_base_config()), encoding="utf-8")
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )

    response = await get_config_sources(
        _="local-dev-token",
        request_id="req-config-no-path",
        config_service=service,
    )

    assert response.data is not None
    dumped = json.dumps(
        [source.vrn for source in response.data.sources], ensure_ascii=False
    )
    assert str(tmp_path) not in dumped
    assert str(Path.cwd() / "configs") not in dumped
    assert response.data.sources[0].vrn.startswith("boxteam://inline/")
    assert response.data.sources[0].vrn.endswith("/resources/config/workspace_inline")
    assert response.data.sources[1].vrn is None
    assert response.data.sources[2].vrn is None
    assert response.data.sources[0].layer == "inline"
    assert response.data.sources[0].precedence == 0
    assert response.data.sources[0].source_key is None
    assert response.data.sources[1].layer == "user"
    assert response.data.sources[1].precedence == 1
    assert response.data.sources[2].layer == "user_local"
    assert response.data.sources[2].precedence == 2
    assert response.data.schema_vrn.startswith("boxteam://inline/")
    assert response.data.schema_vrn.endswith("/resources/config/workspace_schema")
    body = response.model_dump_json()
    assert str(tmp_path) not in body
    assert str(Path.cwd() / "configs") not in body
    assert _REAL_PATH_PATTERN.search(body) is None
    assert '"path"' not in body
    assert '"schema_path"' not in body


_REAL_PATH_PATTERN = re.compile(
    r'(?:^|["\s])(?:[A-Za-z]:\\|/)[^"\s]*\.(?:jsonc|json|sqlite)(?:["\s]|$)'
)


_CUSTOM_SCHEMA = {
    "type": "object",
    "properties": {"config_version": {"type": "integer"}},
    "required": ["config_version"],
}


@pytest.mark.asyncio
async def test_config_sources_schema_vrn_only_for_release_inline_schema(
    tmp_path: Path,
) -> None:
    """非发行包 schema MUST NOT 编 inline VRN：返回 null 且端点不崩。

    点号 stem（`my.company.schema`，非法 VRN 字符）与普通自定义 schema 都走此判据；
    改前实现直接以 `schema_path.stem` 编 inline VRN，点号 stem 会抛 `VrnGrammarError`
    令端点 500。本用例锁定「不可寻址返回 null、任何输入不 500」。
    """

    for schema_name in ("my.company.schema.jsonc", "custom_schema.jsonc"):
        schema_file = tmp_path / schema_name
        schema_file.write_text(json.dumps(_CUSTOM_SCHEMA), encoding="utf-8")
        workspace_dir = tmp_path / schema_name.replace(".", "_")
        workspace_dir.mkdir()
        config_path = workspace_dir / "workspace.jsonc"
        payload = dict(_base_config())
        payload["$schema"] = f"../{schema_name}"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
        )

        response = await get_config_sources(
            _="local-dev-token",
            request_id="req-config-custom-schema",
            config_service=service,
        )

        assert response.data is not None
        assert response.data.schema_vrn is None
        assert str(tmp_path) not in response.model_dump_json()




@pytest.mark.asyncio
async def test_config_endpoint_metadata_never_exposes_real_path(
    tmp_path: Path,
) -> None:
    """缺陷1：``/api/v1/config`` 响应体与 metadata 不得含任何真实路径。"""

    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps(_base_config()), encoding="utf-8")
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(
        json.dumps({"logger": {"level": "debug"}}), encoding="utf-8"
    )
    workspace_root = tmp_path / "workspace"
    workspace_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_path.parent.mkdir(parents=True)
    workspace_path.write_text(
        json.dumps({"agents": {"default": {"name": "Workspace Agent"}}}),
        encoding="utf-8",
    )
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )

    response = await get_config(
        _="local-dev-token",
        request_id="req-config-no-path",
        config_service=service,
    )

    assert response.data is not None
    metadata = response.data.metadata
    # 真实路径位置字段 MUST 整体移除（不得用空串/省略号当替身）。
    assert "config_path" not in metadata
    assert "source_paths" not in metadata
    # 响应体整体不得出现任何真实路径形态（含 tmpdir、仓库 configs 目录、sqlite）。
    dumped = response.model_dump_json()
    for leaked in (str(tmp_path), str(Path.cwd() / "configs"), ".sqlite"):
        assert leaked not in dumped
    assert _REAL_PATH_PATTERN.search(dumped) is None
    # 来源位置仍以 VRN 兄弟字段对外，且只有可寻址层带值。
    sources = metadata["source_details"]
    assert sources[0]["vrn"].startswith("boxteam://inline/")
    assert sources[0]["vrn"].endswith("/resources/config/workspace_inline")
    assert all(source["vrn"] is None for source in sources[1:])
