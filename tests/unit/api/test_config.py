from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from app.api.config import get_config, get_config_sources
from app.core.config_sources import ConfigSourceLayer
from app.schemas.gateway.config import GatewayConfigSourceDTO
from app.schemas.internal_v2.config import ConfigSourceDTO, ConfigSourcesDTO
from app.services.infrastructure.config_service import ConfigService
from configs.installer import (
    install_user_configuration,
    install_workspace_configuration,
)
from configs.runtime import read_jsonc_object


def test_config_source_dtos_share_the_closed_logical_layer_set() -> None:
    expected_layers = (
        "inline",
        "user",
        "user_local",
        "workspace",
        "runtime_override",
        "source",
    )
    assert get_args(ConfigSourceLayer) == expected_layers

    for source_model in (ConfigSourceDTO, GatewayConfigSourceDTO):
        source = source_model(
            uri=None,
            layer="runtime_override",
            precedence=4,
            loaded=True,
        )
        assert source.uri is None
        assert "path" not in source.model_dump()
        assert source.layer == "runtime_override"
        with pytest.raises(ValidationError):
            source_model(uri=None, layer="sqlite", precedence=4, loaded=True)

    sources = ConfigSourcesDTO(revision="rev-1", schema_uri=None)
    assert sources.schema_uri is None
    assert "schema_path" not in sources.model_dump()


def _base_config() -> dict[str, object]:
    return {
        "config_version": 4,
        "extends": "builtin:workspace",
        "settings": {"workspace": {
            "llm": {"providers": {"primary": {
                "config_ref": "#/profiles/primary",
                "models": {"default": {"model": "model-a", "config_ref": "context:provider/primary"}},
            }}},
            "default_agent": "default",
            "agents": {"default": {
                "name": "Default Agent",
                "instructions": {"system_prompt": "hello"},
                "model": {"primary": {"provider_id": "primary", "model_id": "default"}, "fallbacks": []},
            }},
        }},
        "profiles": {"primary": {"extends": "context:workspace", "settings": {"model_options": {
                    "endpoint": "https://example.com/v1",
                    "api_key": "${TEST_API_KEY}",
                    "custom_llm_provider": "openai",
                    "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}},
        }}}},
    }


def _write_config(tmp_path: Path, config: dict[str, object]) -> Path:
    install_user_configuration(config_root=tmp_path, profile="default")
    path = tmp_path / "workspace.jsonc"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_user_config_cannot_fan_out_workspace_plugin_bindings(tmp_path: Path) -> None:
    config = _base_config()
    config["settings"]["workspace"]["plugins"] = {"runtime": {"entries": [{
        "package_id": "sample", "plugin_id": "sample-tool", "package_version": "1.0.0", "enabled": True,
    }]}}
    user_config = _write_config(tmp_path, config)
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=user_config,
    )

    with pytest.raises(ValueError, match="只能写入明确工作区的配置来源"):
        service._get_effective_config()


@pytest.mark.asyncio
async def test_config_sources_endpoint_exposes_layers_and_schema(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path, _base_config())
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace",
        "settings": {"workspace": {"development": {"test_tools": True}}},
    }), encoding="utf-8")
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
    assert response.data.schema_uri.startswith("boxteam://inline/")
    assert response.data.schema_uri.endswith("/resources/config/workspace_schema")
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
    """响应体只含 nullable URI locator 与来源兄弟字段，不泄漏真实路径。"""

    config_path = _write_config(tmp_path, _base_config())
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
        [source.uri for source in response.data.sources], ensure_ascii=False
    )
    assert str(tmp_path) not in dumped
    assert str(Path.cwd() / "configs") not in dumped
    assert response.data.sources[0].uri.startswith("boxteam://inline/")
    assert response.data.sources[0].uri.endswith("/resources/config/workspace_inline")
    assert response.data.sources[1].uri.startswith("boxteam://")
    assert response.data.sources[2].uri.startswith("boxteam://")
    assert response.data.sources[0].layer == "inline"
    assert response.data.sources[0].precedence == 0
    assert response.data.sources[0].source_key == "inline"
    assert response.data.sources[1].layer == "user"
    assert response.data.sources[1].precedence == 1
    assert response.data.sources[2].layer == "user_local"
    assert response.data.sources[2].precedence == 2
    assert response.data.schema_uri.startswith("boxteam://inline/")
    assert response.data.schema_uri.endswith("/resources/config/workspace_schema")
    body = response.model_dump_json()
    assert str(tmp_path) not in body
    assert str(Path.cwd() / "configs") not in body
    assert _REAL_PATH_PATTERN.search(body) is None
    assert '"path"' not in body
    assert '"schema_path"' not in body


_REAL_PATH_PATTERN = re.compile(
    r'(?:^|["\s])(?:[A-Za-z]:\\|/)[^"\s]*\.(?:jsonc|json|sqlite)(?:["\s]|$)'
)


@pytest.mark.asyncio
async def test_config_sources_schema_uri_only_for_release_inline_schema(
    tmp_path: Path,
) -> None:
    """非发行包 schema MUST NOT 编 inline URI：返回 null 且端点不崩。

    点号 stem（`my.company.schema`，非法 URI 字符）与普通自定义 schema 都走此判据；
    改前实现直接以 `schema_path.stem` 编 inline URI，点号 stem 会抛 `ResourceUriGrammarError`
    令端点 500。本用例锁定「不可寻址返回 null、任何输入不 500」。
    """

    for schema_name in ("my.company.schema.jsonc", "custom_schema.jsonc"):
        schema_file = tmp_path / schema_name
        schema = read_jsonc_object(Path("configs/workspace_schema.jsonc"))
        schema["description"] = "自定义 Workspace schema"
        schema_file.write_text(json.dumps(schema), encoding="utf-8")
        workspace_dir = tmp_path / schema_name.replace(".", "_")
        workspace_dir.mkdir()
        config_path = workspace_dir / "workspace.jsonc"
        payload = dict(_base_config())
        payload["$schema"] = f"../{schema_name}"
        config_path = _write_config(workspace_dir, payload)
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
        assert response.data.schema_uri is None
        assert str(tmp_path) not in response.model_dump_json()




@pytest.mark.asyncio
async def test_config_endpoint_metadata_never_exposes_real_path(
    tmp_path: Path,
) -> None:
    """缺陷1：``/api/v1/config`` 响应体与 metadata 不得含任何真实路径。"""

    config_path = _write_config(tmp_path, _base_config())
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace",
        "settings": {"workspace": {"development": {"test_tools": True}}},
    }), encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace_path = install_workspace_configuration(workspace_root=workspace_root)
    workspace_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {"agents": {"default": {"name": "Workspace Agent"}}}},
    }), encoding="utf-8")
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
    # 来源位置仍以 URI 兄弟字段对外，不暴露原生路径。
    sources = metadata["source_details"]
    assert sources[0]["uri"].startswith("boxteam://inline/")
    assert sources[0]["uri"].endswith("/resources/config/workspace_inline")
    assert all(source["uri"].startswith("boxteam://") for source in sources[1:])
