from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.providers.request_parameters import (
    normalize_request_headers,
    normalize_request_parameters,
    validate_configured_parameters,
    validate_extra_body,
)
from app.runtime.model_metadata import apply_model_metadata, model_metadata
from app.schemas.gateway.secrets import ResolvedSecret
from app.schemas.internal_v2.config import (
    ConfigModelInspectionDTO,
    ConfigModelOptionOriginDTO,
    ConfigProviderInspectionDTO,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config.llm_catalog import (
    model_catalog_url,
    public_endpoint,
)
from app.services.infrastructure.config.source_document import (
    inspect_config_nodes,
    redact_source_payload,
)
from configs.inheritance import ConfigGraph
from configs.inheritance.values import ConfigValue, ResolvedNode, json_pointer, thaw
from configs.network import ExplicitProxy, NetworkSettings


class LlmResolution:
    """模型身份来自目录；选项、网络与来源只消费同一快照中的已解析节点。"""

    def __init__(
        self, *, graph_provider: Callable[[], ConfigGraph],
        default_agent_id_provider: Callable[[], str],
        schema_provider: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._graph_provider = graph_provider
        self._default_agent_id_provider = default_agent_id_provider
        self._schema_provider = schema_provider
        self._resolved_validator: Draft202012Validator | None = None
        self._secrets: ContextVar[dict[str, ResolvedSecret] | None] = ContextVar(
            f"llm_secrets_{id(self)}", default=None,
        )

    @contextmanager
    def use_secrets(self, secrets: dict[str, ResolvedSecret]) -> Iterator[None]:
        token = self._secrets.set(secrets)
        try:
            yield
        finally:
            self._secrets.reset(token)

    @staticmethod
    def _workspace(graph: ConfigGraph) -> Mapping[str, ConfigValue]:
        workspace = graph.resolve_scope().settings["workspace"]
        if not isinstance(workspace, Mapping):
            raise TypeError("Workspace 配置命名空间必须是对象")
        return workspace

    def resolve_default_model(self, graph: ConfigGraph) -> str:
        config = self._workspace(graph)
        default_agent_id = self._default_agent_id_provider()
        agents = config["agents"]
        if not isinstance(agents, Mapping) or default_agent_id not in agents:
            raise ValueError(f"default_agent 引用了不存在的 Agent: {default_agent_id}")
        agent = agents[default_agent_id]
        if not isinstance(agent, Mapping) or not isinstance(agent.get("model"), Mapping):
            raise TypeError(f"agent {default_agent_id} 的 model 配置必须是对象")
        primary = ModelReferenceDTO.model_validate(dict(agent["model"]["primary"]))
        return self.resolve_model_reference(graph, primary)["model"]

    def resolve_model_reference(
        self, graph: ConfigGraph, reference: ModelReferenceDTO | Mapping[str, object],
        *, expand_credentials: bool = True,
    ) -> dict[str, Any]:
        model_ref = reference if isinstance(reference, ModelReferenceDTO) else ModelReferenceDTO.model_validate(dict(reference))
        provider = self._provider_definition(graph, model_ref.provider_id)
        models = provider["models"]
        if not isinstance(models, Mapping) or model_ref.model_id not in models:
            raise ValueError(f"ModelReference 引用了不存在的 Model: provider_id={model_ref.provider_id}, model_id={model_ref.model_id}")
        definition = models[model_ref.model_id]
        if not isinstance(definition, Mapping) or not isinstance(definition.get("model"), str) or not definition["model"]:
            raise ValueError(f"Model 缺少有效服务端名称: provider_id={model_ref.provider_id}, model_id={model_ref.model_id}")
        node = graph.resolve_model(model_ref.provider_id, model_ref.model_id)
        resolved = self._node_options(graph, node, model_name=definition["model"])
        resolved["proxy_environment"] = graph.proxy_environment
        if resolved.get("auth") is not None:
            resolved["auth_network"] = thaw(graph.resolve_provider(model_ref.provider_id).settings["network"])
        resolved.update(provider_id=model_ref.provider_id, model_id=model_ref.model_id, model=definition["model"])
        return self._expand_model_credentials(resolved) if expand_credentials else resolved

    def resolve_provider(self, graph: ConfigGraph, provider_id: str) -> dict[str, Any]:
        """发现只消费 Provider 节点，不借用任意 Model。"""
        self._provider_definition(graph, provider_id)
        resolved = self._node_options(graph, graph.resolve_provider(provider_id))
        resolved["proxy_environment"] = graph.proxy_environment
        resolved["provider_id"] = provider_id
        return self._expand_model_credentials(resolved)

    @staticmethod
    def _provider_definition(graph: ConfigGraph, provider_id: str) -> Mapping[str, ConfigValue]:
        providers = LlmResolution._workspace(graph)["llm"]["providers"]
        if not isinstance(providers, Mapping) or provider_id not in providers:
            raise ValueError(f"引用了不存在的 Provider: {provider_id}")
        provider = providers[provider_id]
        if not isinstance(provider, Mapping):
            raise TypeError(f"Provider {provider_id} 必须是对象")
        return provider

    def list_models(self, graph: ConfigGraph) -> list[dict[str, Any]]:
        providers = self._workspace(graph)["llm"]["providers"]
        return [self.resolve_model_reference(graph, ModelReferenceDTO(provider_id=provider_id, model_id=model_id))
                for provider_id, provider in providers.items() for model_id in provider["models"]]

    def get_model(self, reference: ModelReferenceDTO | Mapping[str, object]) -> dict[str, Any]:
        return self.resolve_model_reference(self._graph_provider(), reference)

    def _node_options(self, graph: ConfigGraph, node: ResolvedNode, *, model_name: str | None = None) -> dict[str, Any]:
        options = node.settings["model_options"]
        if not isinstance(options, Mapping):
            raise TypeError("模型节点 model_options 必须是对象")
        resolved = apply_model_metadata(cast(dict[str, object], thaw(options)), model_name, owner_key=graph.owner_id)
        mode = parse_provider_api_mode(resolved)
        request = resolved.get("request_options", {})
        if not isinstance(request, dict):
            raise TypeError("request_options 必须是对象")
        if "parameters" in request:
            request["parameters"] = normalize_request_parameters(request["parameters"], protocol=mode.protocol)
            validate_configured_parameters(request["parameters"])
            validate_extra_body(request["parameters"], protocol=mode.protocol)
        if "default_headers" in request:
            request["default_headers"] = normalize_request_headers(request["default_headers"])
        if self._resolved_validator is None:
            self._resolved_validator = Draft202012Validator({
                "$ref": "#/$defs/resolvedModelOptions", "$defs": self._schema_provider()["$defs"],
            }, registry=Registry())
        try:
            self._resolved_validator.validate(resolved)
        except ValidationError as error:
            raise ValueError(f"模型选项校验失败: field={json_pointer(tuple(str(key) for key in error.absolute_path))}, rule={error.validator}") from error
        resolved["network"] = thaw(node.settings["network"])
        return resolved

    def inspect_model_reference(self, graph: ConfigGraph, reference: ModelReferenceDTO) -> ConfigModelInspectionDTO:
        resolved = self.resolve_model_reference(graph, reference, expand_credentials=False)
        definition = self._provider_definition(graph, reference.provider_id)["models"][reference.model_id]
        node = graph.resolve_model(reference.provider_id, reference.model_id)
        return ConfigModelInspectionDTO(
            model_ref=reference, model=resolved["model"],
            **self._inspect_node(graph, node, resolved, config_ref=definition["config_ref"]),
        )

    def inspect_provider(self, graph: ConfigGraph, provider_id: str) -> ConfigProviderInspectionDTO:
        definition = self._provider_definition(graph, provider_id)
        node = graph.resolve_provider(provider_id)
        resolved = self._node_options(graph, node)
        return ConfigProviderInspectionDTO(provider_id=provider_id,
            **self._inspect_node(graph, node, resolved, config_ref=definition["config_ref"]))

    def _inspect_node(self, graph: ConfigGraph, node: ResolvedNode, resolved: dict[str, Any], *, config_ref: str) -> dict[str, Any]:
        options = {key: value for key, value in resolved.items()
                   if key not in {"provider_id", "model_id", "model", "credential_source", "network", "proxy_environment", "proxy_routes", "auth_network", "auth_proxy_routes"}}
        origins: list[ConfigModelOptionOriginDTO] = []
        catalog = model_metadata(resolved["custom_llm_provider"], resolved["model"], owner_key=graph.owner_id) if "model" in resolved else None

        def visit(value: object, path: tuple[str, ...]) -> None:
            if isinstance(value, dict) and value:
                for key, item in value.items():
                    visit(item, (*path, key))
                return
            canonical = (path[:2] + ("max_output_tokens",) + path[3:]
                         if path[:2] == ("request_options", "parameters") and len(path) >= 3 and path[2] == "max_tokens" else path)
            try:
                origin = node.origins.locate(("model_options", *canonical))
            except KeyError:
                if path[:2] not in {("api_mode", "model_info"), ("api_mode", "supports_reasoning")}:
                    raise ValueError(f"模型选项缺少可核对来源: {json_pointer(path)}") from None
                if path == ("api_mode", "model_info", "supports_reasoning") and "supports_reasoning" in node.settings["model_options"].get("api_mode", {}):
                    origins.append(ConfigModelOptionOriginDTO(path=json_pointer(path), source_key="api_mode.supports_reasoning",
                        definition="builtin", definition_path="/api_mode/supports_reasoning"))
                    return
                automatic = bool(catalog) and (path[:2] == ("api_mode", "supports_reasoning") or len(path) == 3 and path[2] in catalog)
                origins.append(ConfigModelOptionOriginDTO(path=json_pointer(path), source_key="LiteLLM" if automatic else "builtin",
                    definition="catalog" if automatic else "builtin", definition_path=resolved["custom_llm_provider"] + "/" + resolved.get("model", "")))
            else:
                origins.append(ConfigModelOptionOriginDTO(path=json_pointer(path), source_key=graph.source_keys[origin.source_uri],
                    definition="node", node_id=origin.node_id, definition_path=origin.pointer))

        visit(options, ())
        options["endpoint"] = public_endpoint(options["endpoint"])
        chain = inspect_config_nodes(graph, node.chain)
        local = graph.local_settings(node)
        inherited = node.parent.settings if node.parent is not None else {}

        def public_settings(settings: Mapping[str, ConfigValue]) -> dict[str, object]:
            selected = {key: thaw(value) for key, value in settings.items() if key in {"network", "model_options"}}
            return cast(dict[str, object], redact_source_payload({"settings": selected})["settings"])

        providers = self._workspace(graph)["llm"]["providers"]
        selected_count = 0
        for provider_id, provider in providers.items():
            selected_count += graph.resolve_provider(provider_id).node_id == node.node_id
            selected_count += sum(graph.resolve_model(provider_id, model_id).node_id == node.node_id
                                  for model_id in provider["models"])
        return {"config_ref": config_ref, "node_id": node.node_id, "chain": chain, "shared": selected_count > 1,
            "local_settings": public_settings(local), "inherited_settings": public_settings(inherited),
            "options": redact_source_payload(options, ("settings", "model_options")), "origins": origins,
            "network": public_settings(node.settings)["network"],
            "catalog_url": None if resolved.get("auth") is not None else public_endpoint(str(model_catalog_url(resolved)))}

    def expand_model(self, model: Mapping[str, Any]) -> dict[str, Any]:
        """在已绑定的执行凭据作用域内展开单个已解析模型。"""
        return self._expand_model_credentials(dict(model))

    def _expand_model_credentials(self, model: dict[str, Any]) -> dict[str, Any]:
        api_key = model.get("api_key", "")
        if not isinstance(api_key, str):
            raise TypeError("Model api_key 必须是字符串")
        expanded = model.copy()
        if api_key.startswith("secret:"):
            expanded["credential_source"] = api_key
        elif api_key.startswith("${") and api_key.endswith("}"):
            expanded["credential_source"] = "env:" + api_key[2:-1]
        elif "auth" not in model and api_key == "" and "credential_source" not in model:
            expanded["credential_source"] = "none"
        prepared = self._secrets.get()
        if prepared is None:
            return expanded
        if api_key.startswith("secret:"):
            secret_id = api_key.removeprefix("secret:")
            if secret_id not in prepared:
                raise ValueError(f"模型所需命名密钥未准备: {secret_id}")
            expanded["api_key"] = prepared[secret_id].value.get_secret_value()
        elif api_key.startswith("${") and api_key.endswith("}"):
            var_name = api_key[2:-1]
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ValueError(f"环境变量 {var_name} 未设置")
            expanded["api_key"] = env_value
        from app.runtime.network_proxy import ProxyRoutes

        for network_key, routes_key in (("network", "proxy_routes"), ("auth_network", "auth_proxy_routes")):
            if network_key not in model:
                continue
            proxy = NetworkSettings.model_validate(model[network_key]).proxy
            password = None
            if isinstance(proxy, ExplicitProxy) and proxy.auth is not None:
                secret_id = proxy.auth.password.removeprefix("secret:")
                if secret_id not in prepared:
                    raise ValueError(f"代理所需命名密钥未准备: {secret_id}")
                password = prepared[secret_id].value.get_secret_value()
            expanded[routes_key] = ProxyRoutes.from_configuration(proxy, environment=model["proxy_environment"], password=password)
        return expanded


__all__ = ["LlmResolution"]
