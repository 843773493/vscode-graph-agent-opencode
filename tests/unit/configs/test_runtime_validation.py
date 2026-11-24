from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from app.services.infrastructure.config.source_uri import config_file_uri
from configs.inheritance import ConfigGraph
from configs.installer import install_user_configuration
from configs.runtime import (
    merge_json_objects,
    read_jsonc_object,
    validate_config,
    validate_config_document_payload,
    validate_config_payload,
)


@pytest.fixture(scope="module")
def workspace_schema() -> dict[str, object]:
    return read_jsonc_object(Path.cwd() / "configs/workspace_schema.jsonc")


@pytest.fixture
def workspace_graph(tmp_path: Path, workspace_schema: dict[str, object]) -> ConfigGraph:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_root = tmp_path / "home/config"
    install_user_configuration(config_root=config_root, profile="default", workspace_root=workspace)
    entry = workspace / ".boxteam/workspace.jsonc"
    aliases = {
        "builtin:workspace": Path.cwd() / "configs/workspace_inline.jsonc",
        "user:workspace": config_root / "workspace.jsonc",
        "user:workspace_local": config_root / "workspace_local.jsonc",
    }
    names = {path: alias.replace(":", "_") for alias, path in aliases.items()}
    names[entry] = "workspace_root"
    return ConfigGraph(
        domain="workspace", version=4, owner_id="workspace-test", entry=entry, aliases=aliases,
        settings_schema={"$ref": "#/$defs/effectiveSettings", "$defs": workspace_schema["$defs"]},
        options_schema={"$ref": "#/$defs/effectiveOptions", "$defs": workspace_schema["$defs"]},
        source_key=lambda path: names[path],
        source_uri=lambda path: config_file_uri(scope="user", scope_id="local", logical_name=names[path]),
        authorize_source=lambda path: path in names,
    )


def test_workspace_initialization_resolves_the_written_chain_and_all_model_nodes(workspace_graph: ConfigGraph) -> None:
    scope = workspace_graph.resolve_scope()
    assert len(scope.chain) == 4
    assert scope.settings["model_options"]["api_mode"]["streaming"] is True
    for provider_id, provider in scope.settings["workspace"]["llm"]["providers"].items():
        workspace_graph.resolve_provider(provider_id)
        for model_id in provider["models"]:
            options = workspace_graph.resolve_model(provider_id, model_id).settings
            assert options["model_options"]["api_mode"]["streaming"] is True
    assert workspace_graph.resolve_provider("DeepSeek-V4.1-Flash").settings["network"]["proxy"]["mode"] == "direct"


def test_workspace_document_and_flat_payload_require_their_explicit_validation_port(
    workspace_schema: dict[str, object],
) -> None:
    document = read_jsonc_object(Path.cwd() / "configs/workspace_inline.jsonc")
    payload = {**document["settings"]["workspace"], "network": document["settings"]["network"], "config_version": 4}
    assert validate_config_document_payload(document, schema=workspace_schema) is document
    assert validate_config_payload(payload, schema=workspace_schema) is payload
    with pytest.raises(ValueError, match="配置验证失败"):
        validate_config_payload(document, schema=workspace_schema)
    with pytest.raises(ValueError, match="配置验证失败"):
        validate_config_document_payload(payload, schema=workspace_schema)


def test_connection_options_allow_metadata_to_be_filled_before_final_validation(workspace_schema: dict[str, object]) -> None:
    options = {"endpoint": "https://example.test/v1", "api_key": "", "custom_llm_provider": "openai",
               "api_mode": {"protocol": "chat_completions"}}
    definitions = workspace_schema["$defs"]
    jsonschema.validate(options, {"$ref": "#/$defs/connectionModelOptions", "$defs": definitions})
    final = {"$ref": "#/$defs/resolvedModelOptions", "$defs": definitions}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(options, final)
    options["api_mode"].update(model_info={}, supports_reasoning={})
    jsonschema.validate(options, final)
    options["request_options"] = {"no_proxy": True}
    with pytest.raises(jsonschema.ValidationError, match="no_proxy"):
        jsonschema.validate(options, final)


def test_workspace_v4_schema_replaces_parameter_objects_and_keeps_header_merge(workspace_schema: dict[str, object]) -> None:
    schema = {"$ref": "#/$defs/effectiveSettings", "$defs": workspace_schema["$defs"]}
    merged = merge_json_objects(
        {"model_options": {"request_options": {"parameters": {"extra_body": {"parent": True, "keep": 1}},
                                               "default_headers": {"X-Parent": "kept"}}}},
        {"model_options": {"request_options": {"parameters": {"extra_body": {"keep": 0}},
                                               "default_headers": {"X-Child": "added"}}}},
        schema=schema,
    )
    assert merged["model_options"]["request_options"] == {
        "parameters": {"extra_body": {"keep": 0}}, "default_headers": {"X-Parent": "kept", "X-Child": "added"},
    }


@pytest.mark.parametrize("native", ["/private/config/secret-marker", r"C:\private\config\secret-marker"])
@pytest.mark.parametrize("case", ["value", "key", "dynamic", "schema", "policy"])
def test_validation_rejects_without_echoing_input_or_schema(
    tmp_path: Path, native: str, case: str,
) -> None:
    schema: dict[str, object] = {"type": "object"}
    config: dict[str, object] = {}
    if case == "value":
        schema["properties"] = {"field": {"type": "integer"}}
        config["field"] = native
    elif case == "key":
        schema["additionalProperties"] = False
        config[native] = "unused"
    elif case == "dynamic":
        schema["additionalProperties"] = {"type": "array", "items": {"type": "integer"}}
        config[native] = [native]
    elif case == "schema":
        schema["type"] = native
    else:
        schema["x-boxteam-config-domain"] = "gateway"
        schema["x-boxteam-policy-manifest"] = [native]
    schema_path = tmp_path / "schema.jsonc"
    schema_bytes = json.dumps(schema).encode()
    schema_path.write_bytes(schema_bytes)
    before = json.dumps(config)

    with pytest.raises(ValueError, match="配置.*失败") as failure:
        validate_config(config, schema_path=schema_path)

    assert "secret-marker" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)
    cause = failure.value.__cause__
    assert cause is not None
    assert "secret-marker" in str(cause)
    if case != "policy":
        assert isinstance(cause, (jsonschema.ValidationError, jsonschema.SchemaError))
        assert "rule=" in str(failure.value)
        assert "location=" in str(failure.value)
    assert json.dumps(config) == before
    assert schema_path.read_bytes() == schema_bytes


def test_validation_returns_the_original_valid_config(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.jsonc"
    schema_path.write_text('{"type":"object","properties":{"field":{"type":"integer"}}}')
    config: dict[str, object] = {"field": 7}
    assert validate_config(config, schema_path=schema_path) is config


@pytest.mark.parametrize("reference_kind", ["fragment", "file"])
def test_unresolved_schema_reference_does_not_echo_reference(tmp_path: Path, reference_kind: str) -> None:
    reference = "#/$defs/secret-marker" if reference_kind == "fragment" else (tmp_path / "secret-marker.json").as_uri()
    schema_path = tmp_path / "schema.jsonc"
    schema_path.write_text(json.dumps({"$ref": reference}))
    with pytest.raises(ValueError, match="stage=schema_reference") as failure:
        validate_config({}, schema_path=schema_path)
    assert "secret-marker" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)
    assert failure.value.__cause__ is not None
    assert reference in str(failure.value.__cause__)
