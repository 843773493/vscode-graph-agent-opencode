from __future__ import annotations

from copy import deepcopy

import pytest

from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config.llm_resolution import LlmResolution
from app.services.infrastructure.config.source_document import source_origins
from configs.runtime import merge_json_objects


@pytest.fixture
def model_config() -> dict:
    return {"llm": {"providers": {"team": {
        "defaults": {
            "endpoint": "https://models.example/v1", "custom_llm_provider": "openai",
            "api_key": "${BOXTEAM_UNIT_MISSING_KEY}",
            "api_mode": {"protocol": "responses", "streaming": False,
                         "model_info": {"supports_function_calling": True, "supports_reasoning": True},
                         "supports_reasoning": {"reasoning_items": {"summary": True, "encrypted_content": True}}},
            "request_options": {"parameters": {"temperature": 1, "max_tokens": 100,
                                               "extra_body": {"left": True, "nullable": None}},
                                "default_headers": {"X-Route": "default"}},
        },
        "models": {"general": {"model": "upstream-general", "overrides": {
            "request_options": {"parameters": {"temperature": 0, "max_output_tokens": None,
                                               "extra_body": {"right": True}},
                                "default_headers": {"x-route": "model"}},
        }}},
    }}}}


def resolve(config: dict, workspace_config_schema: dict) -> dict:
    return LlmResolution(effective_config_provider=lambda: config, default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema).get_model(
        {"provider_id": "team", "model_id": "general"},
    )


@pytest.fixture
def known_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.runtime import model_metadata

    monkeypatch.setattr(model_metadata, "local_model_catalog", lambda: {"gpt-6-luna": {
        "litellm_provider": "openai", "mode": "chat", "supports_vision": True,
        "supports_function_calling": True, "supports_reasoning": True, "max_input_tokens": 922000,
    }})


def test_catalog_capabilities_are_defaults_for_real_model_reference(
    model_config: dict, workspace_config_schema: dict, known_model_catalog: None,
) -> None:
    provider = model_config["llm"]["providers"]["team"]
    provider["defaults"]["api_mode"] = {"protocol": "responses"}
    provider["defaults"].pop("request_options")
    provider["models"]["general"] = {"model": "gpt-6-luna"}
    original = deepcopy(model_config)
    resolver = LlmResolution(effective_config_provider=lambda: model_config,
        default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema)
    resolved = resolver.get_model({"provider_id": "team", "model_id": "general"})
    assert resolved["api_mode"]["model_info"] == {
        "supports_vision": True, "supports_function_calling": True,
        "supports_reasoning": True, "max_input_tokens": 922000,
    }
    assert resolved["api_mode"]["supports_reasoning"]["reasoning_items"]["encrypted_content"] is True
    assert "request_options" not in resolved
    origins = {str(item["path"]): str(item["source_key"]) for item in source_origins({"inline": model_config}, model_config, schema=workspace_config_schema)}
    inspected = resolver.inspect_model_reference(model_config,
        ModelReferenceDTO(provider_id="team", model_id="general"), origins)
    assert inspected.options["api_mode"] == resolved["api_mode"]
    assert any(item.definition == "catalog" and item.path.endswith("max_input_tokens") for item in inspected.origins)
    assert model_config == original

    # 只声明回放字段时，派生的思考标志不能假装来自某个不存在的配置叶子。
    provider["defaults"]["api_mode"]["supports_reasoning"] = {"reasoning_items": {"summary": True}}
    origins = {str(item["path"]): str(item["source_key"]) for item in source_origins({"inline": model_config}, model_config, schema=workspace_config_schema)}
    inspected = resolver.inspect_model_reference(model_config,
        ModelReferenceDTO(provider_id="team", model_id="general"), origins)
    inferred = next(item for item in inspected.origins if item.path == "/api_mode/model_info/supports_reasoning")
    assert inferred.definition == "builtin" and inferred.source_key == "api_mode.supports_reasoning"


def test_explicit_capabilities_override_catalog_and_unknown_model_stays_usable(
    model_config: dict, workspace_config_schema: dict, known_model_catalog: None,
) -> None:
    provider = model_config["llm"]["providers"]["team"]
    provider["defaults"]["api_mode"] = {"protocol": "responses", "model_info": {"supports_vision": False, "max_input_tokens": 64000}}
    provider["models"]["general"] = {"model": "gpt-6-luna", "overrides": {"api_mode": {
        "model_info": {"supports_reasoning": False, "max_input_tokens": 4096}, "supports_reasoning": {},
    }}}
    info = resolve(model_config, workspace_config_schema)["api_mode"]["model_info"]
    assert info["supports_vision"] is False and info["supports_reasoning"] is False
    assert info["max_input_tokens"] == 4096 and info["supports_function_calling"] is True
    provider["models"]["general"] = {"model": "unlisted-private-model"}
    resolved = resolve(model_config, workspace_config_schema)
    assert resolved["model"] == "unlisted-private-model"
    assert resolved["api_mode"]["model_info"] == {"supports_vision": False, "max_input_tokens": 64000}


def test_source_defaults_do_not_override_model_explicit_values(model_config: dict, workspace_config_schema: dict) -> None:
    original = deepcopy(model_config)
    higher = {"llm": {"providers": {"team": {"defaults": {
        "request_options": {"parameters": {"temperature": 0.8, "max_output_tokens": 200}},
    }}}}}
    result = resolve(merge_json_objects(model_config, higher, schema=workspace_config_schema), workspace_config_schema=workspace_config_schema)
    assert result["provider_id"] == "team" and result["model_id"] == "general"
    assert result["model"] == "upstream-general"
    assert result["request_options"]["parameters"] == {
        "temperature": 0, "max_output_tokens": None,
        "extra_body": {"left": True, "nullable": None, "right": True},
    }
    assert result["request_options"]["default_headers"] == {"x-route": "model"}
    assert result["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"
    assert model_config == original


@pytest.mark.parametrize("stage", ["source", "model"])
def test_protocol_switch_replaces_whole_reasoning_block(model_config: dict, stage: str, workspace_config_schema: dict) -> None:
    options = {"custom_llm_provider": "anthropic", "api_mode": {
        "protocol": "anthropic_messages", "supports_reasoning": {"thinking_blocks": {"thinking": True}},
    }}
    if stage == "source":
        model_config = merge_json_objects(model_config, {"llm": {"providers": {"team": {"defaults": options}}}}, schema=workspace_config_schema)
    else:
        model_config["llm"]["providers"]["team"]["models"]["general"]["overrides"].update(options)
    result = resolve(model_config, workspace_config_schema=workspace_config_schema)
    assert result["api_mode"]["supports_reasoning"] == {"thinking_blocks": {"thinking": True}}
    assert result["api_mode"]["streaming"] is False
    assert result["api_mode"]["model_info"]["supports_function_calling"] is True
    assert result["request_options"]["parameters"]["max_tokens"] is None


def test_explicit_empty_reasoning_replaces_lower_block_and_missing_key_inherits(model_config: dict, workspace_config_schema: dict) -> None:
    overrides = model_config["llm"]["providers"]["team"]["models"]["general"]["overrides"]
    overrides["api_mode"] = {"model_info": {"supports_reasoning": False}, "supports_reasoning": {}}
    assert resolve(model_config, workspace_config_schema=workspace_config_schema)["api_mode"]["supports_reasoning"] == {}
    del overrides["api_mode"]
    assert "reasoning_items" in resolve(model_config, workspace_config_schema=workspace_config_schema)["api_mode"]["supports_reasoning"]


def test_source_aliases_override_but_same_layer_aliases_are_rejected(model_config: dict, workspace_config_schema: dict) -> None:
    options = model_config["llm"]["providers"]["team"]["models"]["general"]["overrides"]
    options["request_options"]["parameters"] = {"max_tokens": 10, "max_output_tokens": 20}
    with pytest.raises(ValueError, match="别名重复"):
        resolve(model_config, workspace_config_schema=workspace_config_schema)


def test_environment_is_resolved_only_inside_execution_scope(model_config: dict, monkeypatch: pytest.MonkeyPatch, workspace_config_schema: dict) -> None:
    monkeypatch.delenv("BOXTEAM_UNIT_MISSING_KEY", raising=False)
    resolver = LlmResolution(effective_config_provider=lambda: model_config, default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema)
    assert resolver.list_models(model_config)[0]["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"
    with resolver.use_secrets({}), pytest.raises(ValueError, match="未设置"):
        resolver.list_models(model_config)
    monkeypatch.setenv("BOXTEAM_UNIT_MISSING_KEY", "execution-only")
    with resolver.use_secrets({}):
        assert resolver.list_models(model_config)[0]["api_key"] == "execution-only"
    assert resolver.list_models(model_config)[0]["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"


def test_source_header_case_is_merged_but_duplicate_same_layer_fails(model_config: dict, workspace_config_schema: dict) -> None:
    higher = {"llm": {"providers": {"team": {"defaults": {
        "request_options": {"default_headers": {"x-route": "higher"}},
    }}}}}
    merged = merge_json_objects(model_config, higher, schema=workspace_config_schema)
    assert merged["llm"]["providers"]["team"]["defaults"]["request_options"]["default_headers"] == {"x-route": "higher"}
    higher["llm"]["providers"]["team"]["defaults"]["request_options"]["default_headers"]["X-Route"] = "duplicate"
    with pytest.raises(ValueError, match="大小写重复"):
        merge_json_objects(model_config, higher, schema=workspace_config_schema)


def test_inspection_reports_original_alias_and_header_sources_without_expanding_credentials(
    model_config: dict, monkeypatch: pytest.MonkeyPatch, workspace_config_schema: dict,
) -> None:
    from app.runtime import litellm_runtime

    def forbidden():
        pytest.fail("本地模型预检不能启动公共目录刷新或等待网络")

    monkeypatch.setattr(litellm_runtime, "load_litellm", forbidden)
    original = deepcopy(model_config)
    higher = {"llm": {"providers": {"team": {"defaults": {
        "request_options": {"parameters": {"max_output_tokens": 200}},
    }}}}}
    config = merge_json_objects(model_config, higher, schema=workspace_config_schema)
    resolver = LlmResolution(effective_config_provider=lambda: config, default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema)
    # 作用域中缺少执行所需环境变量；管理预检仍只返回引用，不能尝试展开它。
    with resolver.use_secrets({}):
        result = resolver.inspect_model_reference(config, ModelReferenceDTO(provider_id="team", model_id="general"),
            {str(origin["path"]): str(origin["source_key"])
             for origin in source_origins({"inline": model_config, "workspace": higher}, config, schema=workspace_config_schema)})
    origins = {origin.path: origin for origin in result.origins}
    assert result.options["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"
    assert result.options["request_options"]["parameters"]["max_output_tokens"] is None
    origin = origins["/request_options/parameters/max_output_tokens"]
    assert origin.source_key == "inline" and origin.definition == "overrides"
    assert origin.definition_path.endswith("/models/general/overrides/request_options/parameters/max_output_tokens")
    assert origins["/request_options/default_headers/x-route"].definition_path.endswith("/default_headers/x-route")
    assert origins["/request_options/parameters/extra_body/left"].definition == "defaults"
    assert origins["/request_options/parameters/extra_body/right"].definition == "overrides"
    assert model_config == original


@pytest.mark.parametrize("lower_name,higher_name", [
    ("max_tokens", "max_output_tokens"), ("max_output_tokens", "max_tokens"),
])
def test_inspection_alias_and_header_origins_use_actual_winning_source_keys(
    model_config: dict, lower_name: str, higher_name: str, workspace_config_schema: dict,
) -> None:
    model_config["llm"]["providers"]["team"]["models"]["general"].pop("overrides")
    model_config["llm"]["providers"]["team"]["defaults"]["request_options"]["parameters"] = {lower_name: 100}
    higher = {"llm": {"providers": {"team": {"defaults": {
        "request_options": {"parameters": {higher_name: 50}, "default_headers": {"x-ROUTE": "higher"}},
    }}}}}
    config = merge_json_objects(model_config, higher, schema=workspace_config_schema)
    raw_options = config["llm"]["providers"]["team"]["defaults"]["request_options"]
    assert raw_options["parameters"] == {higher_name: 50}
    assert raw_options["default_headers"] == {"x-ROUTE": "higher"}
    source_entries = source_origins({"inline": model_config, "workspace": higher}, config, schema=workspace_config_schema)
    parameter_origin = next(origin for origin in source_entries if origin["path"].endswith(f"/parameters/{higher_name}"))
    assert parameter_origin["source_key"] == "workspace" and parameter_origin["shadowed_sources"] == ["inline"]
    header_origin = next(origin for origin in source_entries if origin["path"].endswith("/default_headers/x-ROUTE"))
    assert header_origin["source_key"] == "workspace" and header_origin["shadowed_sources"] == ["inline"]
    resolver = LlmResolution(effective_config_provider=lambda: config, default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema)
    result = resolver.inspect_model_reference(config, ModelReferenceDTO(provider_id="team", model_id="general"),
        {str(origin["path"]): str(origin["source_key"]) for origin in source_entries})
    origins = {origin.path: origin for origin in result.origins}
    assert result.options["request_options"]["parameters"]["max_output_tokens"] == 50
    assert origins["/request_options/parameters/max_output_tokens"].source_key == "workspace"
    assert origins["/request_options/parameters/max_output_tokens"].definition_path.endswith(f"/parameters/{higher_name}")
    assert origins["/request_options/default_headers/x-route"].definition_path.endswith("/default_headers/x-ROUTE")


def test_oauth_inspection_does_not_construct_authentication_owner(model_config: dict, monkeypatch: pytest.MonkeyPatch, workspace_config_schema: dict) -> None:
    from app.runtime.litellm_runtime import _import_litellm

    _import_litellm()
    from litellm.llms.chatgpt.responses import transformation

    def forbidden():
        pytest.fail("只读地址预检不能创建 OAuth 目录或认证 owner")

    monkeypatch.setattr(transformation, "Authenticator", forbidden)
    defaults = model_config["llm"]["providers"]["team"]["defaults"]
    defaults.update(endpoint="https://chatgpt.com/backend-api/codex/", custom_llm_provider="chatgpt",
        auth={"type": "oauth", "method": "chatgpt"})
    defaults.pop("api_key")
    resolver = LlmResolution(effective_config_provider=lambda: model_config, default_agent_id_provider=lambda: "default", schema_provider=lambda: workspace_config_schema)
    result = resolver.inspect_model_reference(model_config, ModelReferenceDTO(provider_id="team", model_id="general"),
        {str(origin["path"]): str(origin["source_key"])
         for origin in source_origins({"inline": model_config}, model_config, schema=workspace_config_schema)})
    assert result.request_url == "https://chatgpt.com/backend-api/codex/responses"
    assert result.catalog_url is None
