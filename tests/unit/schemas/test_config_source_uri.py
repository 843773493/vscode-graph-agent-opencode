"""配置来源在普通输出和冷快照恢复边界拒绝原生路径，不修改原值。"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.gateway.config.sources import _gateway_persisted_source_details
from app.schemas.gateway.config import GatewayConfigSourceDTO, GatewayConfigSourcesDTO
from app.schemas.internal_v2.config import ConfigSourceDTO, ConfigSourcesDTO
from app.services.infrastructure.config_service.config_snapshot import (
    ConfigSnapshotMixin,
)


@pytest.fixture(params=[
    "/private-config/workspace.jsonc", r"C:\private-config\workspace.jsonc",
    r"\\server\private-config\workspace.jsonc", "file:///private-config/workspace.jsonc",
    "workspace.jsonc", "", "boxteam://workspace/ws_a/resources/files/root/workspace.jsonc",
])
def invalid_uri(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.mark.parametrize("source_type", [ConfigSourceDTO, GatewayConfigSourceDTO])
def test_source_output_rejects_invalid_uri_without_echoing_input(
    source_type: type[ConfigSourceDTO | GatewayConfigSourceDTO], invalid_uri: str,
) -> None:
    with pytest.raises(ValidationError) as rejected:
        source_type(uri=invalid_uri, layer="inline", precedence=0, loaded=True)
    assert rejected.value.errors(include_input=False)[0]["loc"] == ("uri",)
    assert "input_value=" not in str(rejected.value)
    if invalid_uri:
        assert invalid_uri not in str(rejected.value)


@pytest.mark.parametrize("result_type", [ConfigSourcesDTO, GatewayConfigSourcesDTO])
@pytest.mark.parametrize("field", ["schema_uri", "nested_source"])
def test_source_envelope_rejects_schema_and_nested_source_uri(
    result_type: type[ConfigSourcesDTO | GatewayConfigSourcesDTO], field: str, invalid_uri: str,
) -> None:
    body: dict[str, object] = {"revision": "revision-1", "schema_uri": None, "sources": []}
    if field == "schema_uri":
        body["schema_uri"] = invalid_uri
        expected = ("schema_uri",)
    else:
        body["sources"] = [{"uri": invalid_uri, "layer": "inline", "precedence": 0, "loaded": True}]
        expected = ("sources", 0, "uri")
    before = deepcopy(body)
    with pytest.raises(ValidationError) as rejected:
        result_type.model_validate(body)
    assert rejected.value.errors(include_input=False)[0]["loc"] == expected
    assert "input_value=" not in str(rejected.value) and body == before


@pytest.mark.parametrize("owner", ["workspace", "gateway"])
def test_cold_source_baseline_rejects_invalid_uri_without_repair(owner: str, invalid_uri: str) -> None:
    baseline: dict[str, object] = {"inline:0": {"uri": invalid_uri, "presence": "present"}}
    before = deepcopy(baseline)
    with pytest.raises(ValueError) as rejected:
        if owner == "workspace":
            ConfigSnapshotMixin._persisted_source_details(baseline)
        else:
            _gateway_persisted_source_details(baseline)
    if invalid_uri:
        assert invalid_uri not in str(rejected.value)
    assert baseline == before


@pytest.mark.parametrize("result_type", [ConfigSourcesDTO, GatewayConfigSourcesDTO])
@pytest.mark.parametrize("uri", [
    None, "boxteam://inline/boxteam_v1/resources/config/workspace_inline",
    "boxteam://gateway_remote/workspace/ws_a/resources/config/workspace_override",
])
def test_source_output_preserves_dangling_uri_null_and_revision_siblings(
    result_type: type[ConfigSourcesDTO | GatewayConfigSourcesDTO], uri: str | None,
) -> None:
    source = {"uri": uri, "layer": "inline", "precedence": 0, "loaded": False,
              "layer_revision": 7, "layer_digest": "digest-a", "source_generation": 9}
    result = result_type.model_validate({"revision": "revision-1", "schema_uri": uri, "sources": [source]})
    serialized = result.model_dump(mode="json")
    assert serialized["schema_uri"] == uri
    assert {key: serialized["sources"][0][key] for key in source} == source
    assert result_type.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("source_type", [ConfigSourceDTO, GatewayConfigSourceDTO])
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_copied_source_rejects_invalid_uri_at_publication(
    source_type: type[ConfigSourceDTO | GatewayConfigSourceDTO], invalid_uri: str,
    mode: Literal["python", "json", "json-text"],
) -> None:
    source = source_type(uri=None, layer="runtime_override", precedence=4, loaded=True)
    broken = source.model_copy(update={"uri": invalid_uri})
    with pytest.raises(PydanticSerializationError):
        if mode == "json-text":
            broken.model_dump_json()
        else:
            broken.model_dump(mode=mode)
    assert source.uri is None


@pytest.mark.parametrize("result_type", [ConfigSourcesDTO, GatewayConfigSourcesDTO])
@pytest.mark.parametrize("field", ["schema_uri", "nested_source"])
@pytest.mark.parametrize("mode", ["python", "json", "json-text"])
def test_copied_envelope_and_nested_source_reject_invalid_uri_at_publication(
    result_type: type[ConfigSourcesDTO | GatewayConfigSourcesDTO], invalid_uri: str,
    field: str, mode: Literal["python", "json", "json-text"],
) -> None:
    result = result_type.model_validate({"revision": "r1", "schema_uri": None, "sources": [
        {"uri": None, "layer": "runtime_override", "precedence": 4, "loaded": True},
    ]})
    before = result.model_dump_json()
    if field == "schema_uri":
        broken = result.model_copy(update={"schema_uri": invalid_uri})
    else:
        broken = result.model_copy(deep=True)
        broken.sources[0].uri = invalid_uri
    with pytest.raises(PydanticSerializationError):
        if mode == "json-text":
            broken.model_dump_json()
        else:
            broken.model_dump(mode=mode)
    assert result.model_dump_json() == before


@pytest.mark.parametrize("result_type", [ConfigSourcesDTO, GatewayConfigSourcesDTO])
@pytest.mark.parametrize("uri", [
    None, "boxteam://inline/boxteam_v1/resources/config/workspace_inline",
    "boxteam://gateway_remote/workspace/ws_a/resources/config/workspace_override",
])
def test_copied_source_preserves_original_wire_and_metadata(
    result_type: type[ConfigSourcesDTO | GatewayConfigSourcesDTO], uri: str | None,
) -> None:
    source = {"uri": uri, "layer": "user", "precedence": 2, "loaded": False,
              "layer_revision": 7, "layer_digest": "digest", "source_generation": 9}
    result = result_type.model_validate({"revision": "r1", "schema_uri": uri, "sources": [source]})
    copied = result.model_copy(update={"schema_uri": uri, "sources": [result.sources[0].model_copy(update={"uri": uri})]})
    assert copied.model_dump() == result.model_dump()
    assert copied.model_dump_json() == result.model_dump_json()
