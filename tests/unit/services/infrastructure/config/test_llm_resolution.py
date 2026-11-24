from __future__ import annotations

from copy import deepcopy

import pytest

from app.runtime.provider_model import provider_model_capacity
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config.llm_resolution import LlmResolution

REF = ModelReferenceDTO(provider_id="team", model_id="general")


@pytest.fixture
def model_config() -> dict:
    return {"config_version": 4, "settings": {
        "network": {"proxy": {"mode": "environment"}},
        "model_options": {"api_mode": {"streaming": True}},
        "workspace": {"agents": {}, "llm": {"providers": {"team": {
            "config_ref": "#/profiles/provider", "models": {"general": {
                "model": "upstream-general", "config_ref": "#/profiles/model"}},
        }}}}}, "profiles": {
        "provider": {"extends": "context:workspace", "settings": {"model_options": {
            "endpoint": "https://models.example/v1", "custom_llm_provider": "openai",
            "api_key": "${BOXTEAM_UNIT_MISSING_KEY}",
            "api_mode": {"protocol": "responses", "streaming": False,
                         "model_info": {"supports_function_calling": True, "supports_reasoning": True},
                         "supports_reasoning": {"reasoning_items": {"summary": True, "encrypted_content": True}}},
            "request_options": {"parameters": {"temperature": 1, "max_tokens": 100,
                                               "extra_body": {"left": True, "nullable": None}},
                                "default_headers": {"X-Route": "default"}},
        }}},
        "model": {"extends": "context:provider/team", "settings": {"model_options": {
            "request_options": {"parameters": {"temperature": 0, "max_output_tokens": None,
                                               "extra_body": {"right": True}},
                                "default_headers": {"x-route": "model"}},
        }}},
    }}


@pytest.fixture
def resolver_factory(model_graph_factory, workspace_config_schema: dict):
    def create(document: dict, **kwargs):
        graph = model_graph_factory(document, **kwargs)
        resolver = LlmResolution(graph_provider=lambda: graph, default_agent_id_provider=lambda: "default",
                                 schema_provider=lambda: workspace_config_schema)
        return resolver, graph
    return create


@pytest.fixture
def known_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.runtime import model_metadata
    monkeypatch.setattr(model_metadata, "local_model_catalog", lambda **_kwargs: {"gpt-6-luna": {
        "litellm_provider": "openai", "mode": "chat", "supports_vision": True,
        "supports_function_calling": True, "supports_reasoning": True,
        "max_input_tokens": 922000, "context_window": 1000000,
    }})


def test_catalog_capabilities_are_defaults_for_real_model_reference(model_config, resolver_factory, known_model_catalog):
    options = model_config["profiles"]["provider"]["settings"]["model_options"]
    options["api_mode"] = {"protocol": "responses"}
    options.pop("request_options")
    model_config["profiles"]["model"]["settings"] = {}
    model_config["settings"]["workspace"]["llm"]["providers"]["team"]["models"]["general"]["model"] = "gpt-6-luna"
    original = deepcopy(model_config)
    resolver, graph = resolver_factory(model_config)
    resolved = resolver.get_model(REF)
    assert resolved["api_mode"]["model_info"] == {
        "supports_vision": True, "supports_function_calling": True,
        "supports_reasoning": True, "max_input_tokens": 922000, "context_window": 1000000,
    }
    assert resolved["api_mode"]["supports_reasoning"]["reasoning_items"]["encrypted_content"] is True
    assert "request_options" not in resolved
    inspected = resolver.inspect_model_reference(graph, REF)
    assert inspected.options["api_mode"] == resolved["api_mode"]
    assert any(item.definition == "catalog" and item.path.endswith("max_input_tokens") for item in inspected.origins)
    assert model_config == original
    options["api_mode"]["supports_reasoning"] = {"reasoning_items": {"summary": True, "encrypted_content": False}}
    resolver, graph = resolver_factory(model_config)
    inferred = next(item for item in resolver.inspect_model_reference(graph, REF).origins
                    if item.path == "/api_mode/model_info/supports_reasoning")
    assert inferred.definition == "builtin" and inferred.source_key == "api_mode.supports_reasoning"


def test_explicit_capabilities_override_catalog_and_unknown_model_stays_usable(model_config, resolver_factory, known_model_catalog):
    options = model_config["profiles"]["provider"]["settings"]["model_options"]
    options["api_mode"] = {"protocol": "responses", "model_info": {"supports_vision": False, "max_input_tokens": 64000}}
    identity = model_config["settings"]["workspace"]["llm"]["providers"]["team"]["models"]["general"]
    identity["model"] = "gpt-6-luna"
    local = model_config["profiles"]["model"]["settings"]["model_options"]
    local["api_mode"] = {"model_info": {"supports_reasoning": False, "max_input_tokens": 4096}, "supports_reasoning": {}}
    resolver, _ = resolver_factory(model_config)
    info = resolver.get_model(REF)["api_mode"]["model_info"]
    assert info["supports_vision"] is False and info["supports_reasoning"] is False
    assert info["max_input_tokens"] == 4096 and info["supports_function_calling"] is True
    identity["model"] = "unlisted-private-model"
    local.pop("api_mode")
    resolver, _ = resolver_factory(model_config)
    assert resolver.get_model(REF)["api_mode"]["model_info"] == {"supports_vision": False, "max_input_tokens": 64000}


def test_same_name_providers_keep_distinct_capacity_and_actual_output_aliases(
    model_config, resolver_factory, known_model_catalog, monkeypatch,
):
    """真实节点解析同时保留稳定身份、端点边界与跨层 null／别名。"""
    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider",
                        lambda *_args, **_kwargs: pytest.fail("只读容量不得构造模型"))
    monkeypatch.setattr("app.runtime.litellm_runtime._import_litellm",
                        lambda: pytest.fail("只读容量不得加载 SDK"))
    profiles = model_config["profiles"]
    options = profiles["provider"]["settings"]["model_options"]
    options["api_mode"] = {"protocol": "responses", "model_info": {
        "max_input_tokens": 500000, "context_window": 600000,
    }}
    options["request_options"]["parameters"] = {"max_tokens": 16000}
    profiles["model"]["settings"]["model_options"]["request_options"]["parameters"] = {
        "max_output_tokens": None,
    }
    profiles["smaller-provider"] = deepcopy(profiles["provider"])
    smaller = profiles["smaller-provider"]["settings"]["model_options"]
    smaller["endpoint"] = "https://smaller.example/v1"
    smaller["api_mode"]["model_info"] = {"max_input_tokens": 200000, "context_window": 128000}
    profiles["smaller-model"] = {"extends": "context:provider/smaller", "settings": {
        "model_options": {"request_options": {"parameters": {"max_output_tokens": 8000}}},
    }}
    providers = model_config["settings"]["workspace"]["llm"]["providers"]
    providers["team"]["models"]["general"]["model"] = "gpt-6-luna"
    providers["smaller"] = {"config_ref": "#/profiles/smaller-provider", "models": {"general": {
        "model": "gpt-6-luna", "config_ref": "#/profiles/smaller-model",
    }}}
    resolver, graph = resolver_factory(model_config)
    large = resolver.resolve_model_reference(graph, REF, expand_credentials=False)
    small = resolver.resolve_model_reference(
        graph, ModelReferenceDTO(provider_id="smaller", model_id="general"), expand_credentials=False,
    )
    assert large["model"] == small["model"]
    assert (large["provider_id"], small["provider_id"]) == ("team", "smaller")
    assert large["endpoint"] != small["endpoint"]
    assert large["api_key"] == small["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"
    assert large["request_options"]["parameters"] == {"max_output_tokens": None}
    assert small["request_options"]["parameters"] == {"max_output_tokens": 8000}
    large_capacity, small_capacity = provider_model_capacity(large), provider_model_capacity(small)
    assert large_capacity.input_budget_tokens == 500000
    assert not large_capacity.output_reservation_known
    assert small_capacity.input_budget_tokens == 120000
    assert small_capacity.output_reservation_tokens == 8000


def test_resolved_capacity_evidence_keeps_original_metadata_when_catalog_changes(
    model_config, resolver_factory, known_model_catalog, monkeypatch,
):
    from app.runtime import model_metadata

    options = model_config["profiles"]["provider"]["settings"]["model_options"]
    options["api_mode"] = {"protocol": "responses"}
    options["request_options"]["parameters"] = {}
    model_config["profiles"]["model"]["settings"] = {}
    model_config["settings"]["workspace"]["llm"]["providers"]["team"]["models"]["general"]["model"] = "gpt-6-luna"
    resolver, graph = resolver_factory(model_config)
    frozen_model = resolver.resolve_model_reference(graph, REF, expand_credentials=False)
    before = provider_model_capacity(frozen_model)
    monkeypatch.setattr(model_metadata, "local_model_catalog", lambda **_: {})
    assert provider_model_capacity(frozen_model) == before
    assert before.input_budget_tokens == 922000
    unknown = resolver.resolve_model_reference(graph, REF, expand_credentials=False)
    assert not provider_model_capacity(unknown).capacity_known
    assert provider_model_capacity(unknown).input_budget_tokens is None


def test_child_values_survive_parent_changes_and_object_parameters_replace(model_config, resolver_factory):
    original = deepcopy(model_config)
    model_config["profiles"]["provider"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 0.8
    resolver, graph = resolver_factory(model_config)
    result = resolver.get_model(REF)
    assert (result["provider_id"], result["model_id"], result["model"]) == ("team", "general", "upstream-general")
    assert result["request_options"]["parameters"] == {"temperature": 0, "max_output_tokens": None, "extra_body": {"right": True}}
    assert result["request_options"]["default_headers"] == {"x-route": "model"}
    assert result["network"] == {"proxy": {"mode": "environment"}}
    result["request_options"]["parameters"]["extra_body"]["right"] = False
    assert resolver.get_model(REF)["request_options"]["parameters"]["extra_body"] == {"right": True}
    assert graph.resolve_model("team", "general").settings["model_options"]["request_options"]["parameters"]["temperature"] == 0
    assert original["profiles"]["provider"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] == 1


@pytest.mark.parametrize("stage", ["provider", "model"])
def test_protocol_switch_replaces_whole_reasoning_block(model_config, stage, resolver_factory):
    model_config["profiles"][stage]["settings"]["model_options"].update({"custom_llm_provider": "anthropic", "api_mode": {
        "protocol": "anthropic_messages", "model_info": {"supports_reasoning": True, "supports_function_calling": True}, "supports_reasoning": {"thinking_blocks": {"thinking": True, "redacted_thinking": False}},
    }})
    resolver, _ = resolver_factory(model_config)
    result = resolver.get_model(REF)
    assert result["api_mode"]["supports_reasoning"] == {"thinking_blocks": {"thinking": True, "redacted_thinking": False}}
    assert result["api_mode"]["streaming"] is (stage == "provider")
    assert result["request_options"]["parameters"]["max_tokens"] is None


def test_empty_reasoning_replaces_parent_and_missing_key_inherits(model_config, resolver_factory):
    options = model_config["profiles"]["model"]["settings"]["model_options"]
    options["api_mode"] = {"model_info": {"supports_reasoning": False}, "supports_reasoning": {}}
    resolver, _ = resolver_factory(model_config)
    assert resolver.get_model(REF)["api_mode"]["supports_reasoning"] == {}
    del options["api_mode"]
    resolver, _ = resolver_factory(model_config)
    assert "reasoning_items" in resolver.get_model(REF)["api_mode"]["supports_reasoning"]


def test_aliases_override_across_nodes_but_duplicate_in_one_node_fails(model_config, resolver_factory):
    parameters = model_config["profiles"]["model"]["settings"]["model_options"]["request_options"]["parameters"]
    parameters.update(max_tokens=10, max_output_tokens=20)
    resolver, _ = resolver_factory(model_config)
    with pytest.raises(ValueError, match="别名重复"):
        resolver.get_model(REF)


def test_environment_expands_only_inside_execution_scope(model_config, resolver_factory, monkeypatch):
    monkeypatch.delenv("BOXTEAM_UNIT_MISSING_KEY", raising=False)
    resolver, graph = resolver_factory(model_config)
    assert resolver.list_models(graph)[0]["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"
    with resolver.use_secrets({}), pytest.raises(ValueError, match="未设置"):
        resolver.list_models(graph)
    monkeypatch.setenv("BOXTEAM_UNIT_MISSING_KEY", "execution-only")
    with resolver.use_secrets({}):
        assert resolver.list_models(graph)[0]["api_key"] == "execution-only"
    assert resolver.list_models(graph)[0]["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"


def test_duplicate_header_case_fails_in_same_node(model_config, resolver_factory):
    model_config["profiles"]["model"]["settings"]["model_options"]["request_options"]["default_headers"]["X-ROUTE"] = "duplicate"
    resolver, _ = resolver_factory(model_config)
    with pytest.raises(ValueError, match="大小写重复"):
        resolver.get_model(REF)


@pytest.mark.parametrize("lower_name,higher_name", [("max_tokens", "max_output_tokens"), ("max_output_tokens", "max_tokens")])
def test_inspection_retains_actual_alias_header_sources_without_network(model_config, resolver_factory, monkeypatch, lower_name, higher_name):
    from app.runtime import litellm_runtime
    monkeypatch.setattr(litellm_runtime, "load_litellm", lambda: pytest.fail("预检不能访问网络"))
    provider = model_config["profiles"]["provider"]["settings"]["model_options"]
    provider["request_options"]["parameters"] = {lower_name: 100}
    local = model_config["profiles"]["model"]["settings"]["model_options"]
    local["request_options"] = {"parameters": {higher_name: 50}, "default_headers": {"x-ROUTE": "higher"}}
    resolver, graph = resolver_factory(model_config)
    with resolver.use_secrets({}):
        result = resolver.inspect_model_reference(graph, REF)
    origins = {origin.path: origin for origin in result.origins}
    assert result.options["api_key"] == "${BOXTEAM_UNIT_MISSING_KEY}"
    assert result.options["request_options"]["parameters"]["max_output_tokens"] == 50
    assert origins["/request_options/parameters/max_output_tokens"].definition == "node"
    assert origins["/request_options/parameters/max_output_tokens"].definition_path.endswith(f"/parameters/{higher_name}")
    assert origins["/request_options/default_headers/x-route"].definition_path.endswith("/default_headers/x-ROUTE")
    assert [node.definition_path for node in result.chain] == ["", "/profiles/provider", "/profiles/model"]


def test_model_can_detach_from_provider_and_select_shared_node(model_config, resolver_factory):
    model_config["profiles"]["detached"] = {"extends": "context:workspace", "settings": {"network": {"proxy": {"mode": "direct"}},
        "model_options": {"endpoint": "https://detached.example/v1", "custom_llm_provider": "openai", "api_key": "",
                          "api_mode": {"protocol": "chat_completions"}}}}
    identity = model_config["settings"]["workspace"]["llm"]["providers"]["team"]
    identity["models"]["general"]["config_ref"] = "#/profiles/detached"
    identity["models"]["second"] = {"model": "second", "config_ref": "#/profiles/detached"}
    resolver, graph = resolver_factory(model_config)
    assert resolver.resolve_provider(graph, "team")["endpoint"] == "https://models.example/v1"
    result = resolver.get_model(REF)
    assert result["endpoint"] == "https://detached.example/v1" and result["network"]["proxy"]["mode"] == "direct"
    assert resolver.inspect_model_reference(graph, REF).shared is True
    assert resolver.inspect_provider(graph, "team").shared is False


def test_oauth_inspection_does_not_construct_authentication_owner(model_config, resolver_factory, monkeypatch):
    from app.runtime.litellm_runtime import _import_litellm
    _import_litellm()
    from litellm.llms.chatgpt.responses import transformation
    monkeypatch.setattr(transformation, "Authenticator", lambda: pytest.fail("预检不能创建认证 owner"))
    options = model_config["profiles"]["provider"]["settings"]["model_options"]
    options.update(endpoint="https://chatgpt.com/backend-api/codex/", custom_llm_provider="chatgpt", auth={"type": "oauth", "method": "chatgpt"})
    options.pop("api_key")
    resolver, graph = resolver_factory(model_config)
    result = resolver.inspect_model_reference(graph, REF)
    assert result.request_url == "https://chatgpt.com/backend-api/codex/responses" and result.catalog_url is None


def test_proxy_credentials_prepare_late_even_without_model_api_key(model_config, resolver_factory, monkeypatch):
    from app.schemas.gateway.secrets import ResolvedSecret

    proxy_id = 'sec_' + '1' * 32
    model_config['profiles']['provider']['settings']['model_options']['api_key'] = ''
    model_config['profiles']['model']['settings']['network'] = {'proxy': {
        'mode': 'explicit', 'url': 'http://proxy.example:3128', 'auth': {'username': 'proxy-user', 'password': 'secret:' + proxy_id},
    }}
    resolver, graph = resolver_factory(model_config, environment={'HTTP_PROXY': 'http://old-env.example:3128'})
    model = resolver.get_model(REF)
    assert 'proxy_routes' not in model
    monkeypatch.setenv('HTTP_PROXY', 'http://changed-env.example:3128')
    with resolver.use_secrets({}), pytest.raises(ValueError, match='代理所需命名密钥未准备'):
        resolver.get_model(REF)
    with resolver.use_secrets({proxy_id: ResolvedSecret(revision=1, value='proxy-password')}):
        prepared = resolver.get_model(REF)
    assert prepared['api_key'] == ''
    proxy = prepared['proxy_routes'].routes[0][1]
    assert str(proxy.url) == 'http://proxy.example:3128'
    assert proxy.auth == ('proxy-user', 'proxy-password')
    assert prepared['network']['proxy']['auth']['password'] == 'secret:' + proxy_id
    inspected = resolver.inspect_model_reference(graph, REF)
    assert 'proxy_routes' not in inspected.options and 'proxy_environment' not in inspected.options
    assert inspected.network['proxy']['auth']['password'] == 'secret:' + proxy_id


@pytest.mark.asyncio
async def test_credential_batch_includes_only_selected_api_and_proxy_references(model_config, resolver_factory, monkeypatch):
    from app.schemas.gateway.secrets import ResolvedSecret
    from app.services.infrastructure import gateway_client_common
    from app.services.infrastructure.config_service.config_public import (
        ConfigPublicMixin,
    )
    from app.services.infrastructure.gateway_secrets import client

    key_id, proxy_id = 'sec_' + '1' * 32, 'sec_' + '2' * 32
    model_config['profiles']['provider']['settings']['model_options']['api_key'] = 'secret:' + key_id
    model_config['profiles']['model']['settings']['network'] = {'proxy': {
        'mode': 'explicit', 'url': 'http://proxy.example:3128', 'auth': {'username': 'user', 'password': 'secret:' + proxy_id},
    }}
    resolver, _ = resolver_factory(model_config)
    selected = resolver.get_model(REF)
    batches = []

    class Secrets:
        def __init__(self, connection):
            assert connection == 'target-transport'

        async def resolve(self, references):
            batches.append(references)
            return {reference: ResolvedSecret(revision=1, value='value') for reference in references}

    monkeypatch.setattr(gateway_client_common, 'GatewayTransportConnection', lambda **_kwargs: 'target-transport')
    monkeypatch.setattr(client, 'GatewaySecretsClient', Secrets)
    result = await ConfigPublicMixin.prepare_llm_credentials(object(), [selected, selected])
    assert list(result) == [key_id, proxy_id] and batches == [[key_id, proxy_id]]


def test_environment_routes_use_captured_generation_not_current_process(model_config, resolver_factory, monkeypatch):
    model_config['profiles']['provider']['settings']['model_options']['api_key'] = ''
    environment = {'HTTP_PROXY': 'http://captured.example:3128'}
    resolver, graph = resolver_factory(model_config, environment=environment)
    environment['HTTP_PROXY'] = 'http://changed-input.example:3128'
    monkeypatch.setenv('HTTP_PROXY', 'http://changed-process.example:3128')
    with resolver.use_secrets({}):
        model = resolver.get_model(REF)
    assert str(model['proxy_routes'].routes[0][1].url) == 'http://captured.example:3128'
    assert dict(graph.proxy_environment) == {'HTTP_PROXY': 'http://captured.example:3128'}
