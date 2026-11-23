from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

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
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config.llm_catalog import (
    model_catalog_url,
    public_endpoint,
)
from app.services.infrastructure.config.source_document import redact_source_payload
from configs.runtime import merge_json_objects


class LlmResolution:
    """从生效配置按完整 ModelReference 解析唯一运行时模型。"""

    def __init__(
        self,
        *,
        effective_config_provider: Callable[[], Mapping[str, Any]],
        default_agent_id_provider: Callable[[], str],
        schema_provider: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._effective_config_provider = effective_config_provider
        self._default_agent_id_provider = default_agent_id_provider
        self._schema_provider = schema_provider
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

    def resolve_default_model(self, config: Mapping[str, Any]) -> str:
        default_agent_id = self._default_agent_id_provider()
        agents = config.get("agents", {})
        if not isinstance(agents, Mapping):
            raise TypeError("agents 配置必须是对象")
        if default_agent_id not in agents:
            raise ValueError(f"default_agent 引用了不存在的 Agent: {default_agent_id}")
        agent_config = agents[default_agent_id]
        if not isinstance(agent_config, Mapping):
            raise TypeError(f"agent {default_agent_id} 配置必须是对象")
        model_config = agent_config.get("model", {})
        if not isinstance(model_config, Mapping):
            raise TypeError(f"agent {default_agent_id} 的 model 配置必须是对象")
        primary_value = model_config.get("primary")
        primary = ModelReferenceDTO.model_validate(dict(primary_value) if isinstance(primary_value, Mapping) else primary_value)
        return self.resolve_model_reference(config, primary)["model"]

    def resolve_model_reference(
        self,
        config: Mapping[str, Any],
        reference: ModelReferenceDTO | Mapping[str, object],
        *, expand_credentials: bool = True,
    ) -> dict[str, Any]:
        """先取来源合并后的 Provider，再合并其 defaults 与 Model overrides。"""
        model_ref = (
            reference if isinstance(reference, ModelReferenceDTO)
            else ModelReferenceDTO.model_validate(dict(reference))
        )
        provider = self._provider_definition(config, model_ref.provider_id)
        defaults = provider.get("defaults", {})
        models = provider.get("models")
        if not isinstance(defaults, Mapping) or not isinstance(models, Mapping):
            raise TypeError(f"Provider {model_ref.provider_id} 的 defaults 和 models 必须是对象")
        if model_ref.model_id not in models:
            raise ValueError(
                "ModelReference 引用了不存在的 Model: "
                f"provider_id={model_ref.provider_id}, model_id={model_ref.model_id}"
            )
        model_config = models[model_ref.model_id]
        if not isinstance(model_config, Mapping):
            raise TypeError(f"Model {model_ref.provider_id}/{model_ref.model_id} 必须是对象")
        model_name = model_config.get("model")
        if not isinstance(model_name, str) or not model_name:
            raise ValueError(
                "Model 缺少有效服务端名称: "
                f"provider_id={model_ref.provider_id}, model_id={model_ref.model_id}"
            )
        overrides = model_config.get("overrides", {})
        if not isinstance(overrides, Mapping):
            raise TypeError(
                f"Provider {model_ref.provider_id} 的 Model {model_ref.model_id} overrides 必须是对象"
            )

        resolved = self._merge_model_options(defaults, overrides, model_name=model_name)
        resolved["provider_id"] = model_ref.provider_id
        resolved["model_id"] = model_ref.model_id
        resolved["model"] = model_name
        return self._expand_model_credentials(resolved) if expand_credentials else resolved

    def inspect_model_reference(
        self, config: Mapping[str, Any], reference: ModelReferenceDTO,
        origins: Mapping[str, str],
    ) -> ConfigModelInspectionDTO:
        """仅投影选定模型；复用唯一解析器，来源标注不能解密或另算生效值。"""
        from app.runtime.litellm_runtime import model_request_url

        resolved = self.resolve_model_reference(config, reference, expand_credentials=False)
        provider = self._provider_definition(config, reference.provider_id)
        model = provider["models"][reference.model_id]
        definitions = {"overrides": model.get("overrides", {}), "defaults": provider.get("defaults", {})}

        def pointer(parts: tuple[str, ...]) -> str:
            return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)

        def at(value: object, path: tuple[str, ...]) -> object:
            for key in path:
                if not isinstance(value, Mapping) or key not in value:
                    return missing
                value = value[key]
            return value

        missing = object()
        provider_path = ("llm", "providers", reference.provider_id)
        options = {key: value for key, value in resolved.items()
                   if key not in {"provider_id", "model_id", "model", "credential_source"}}
        option_origins: list[ConfigModelOptionOriginDTO] = []
        catalog_info = model_metadata(resolved["custom_llm_provider"], resolved["model"])

        def original_path(raw: object, path: tuple[str, ...]) -> tuple[str, ...]:
            # 原文键可用参数别名或不同大小写的请求头，定位仍返回实际写入的键。
            values = at(raw, path[:2])
            if not isinstance(values, Mapping) or len(path) < 3:
                return path
            if path[:2] == ("request_options", "parameters"):
                for name, item in values.items():
                    if path[2] in normalize_request_parameters({name: item}, protocol=resolved["api_mode"]["protocol"]):
                        return (*path[:2], name, *path[3:])
            elif path[:2] == ("request_options", "default_headers"):
                for name in values:
                    if name.lower() == path[2].lower():
                        return (*path[:2], name, *path[3:])
            return path

        def visit(value: object, path: tuple[str, ...]) -> None:
            if isinstance(value, dict) and value:
                for key, item in value.items():
                    visit(item, (*path, key))
                return
            for definition, raw in definitions.items():
                raw_path = original_path(raw, path)
                if at(raw, raw_path) is missing:
                    continue
                prefix = ("defaults",) if definition == "defaults" else ("models", reference.model_id, "overrides")
                definition_path = pointer((*provider_path, *prefix, *raw_path))
                source_key = origins.get(definition_path)
                if source_key is not None:
                    option_origins.append(ConfigModelOptionOriginDTO(path=pointer(path), source_key=source_key,
                        definition=definition, definition_path=definition_path))
                    return
                raise ValueError(f"模型选项缺少可核对来源: {pointer(path)}")
            if path[:2] in {("api_mode", "model_info"), ("api_mode", "supports_reasoning")}:
                if path == ("api_mode", "model_info", "supports_reasoning") and any(
                    at(raw, ("api_mode", "supports_reasoning")) is not missing for raw in definitions.values()
                ):
                    option_origins.append(ConfigModelOptionOriginDTO(path=pointer(path),
                        source_key="api_mode.supports_reasoning", definition="builtin",
                        definition_path="/api_mode/supports_reasoning"))
                    return
                catalog_default = bool(catalog_info) and (path[:2] == ("api_mode", "supports_reasoning")
                    or len(path) == 3 and path[2] in catalog_info)
                option_origins.append(ConfigModelOptionOriginDTO(path=pointer(path),
                    source_key="LiteLLM" if catalog_default else "builtin",
                    definition="catalog" if catalog_default else "builtin",
                    definition_path=resolved["custom_llm_provider"] + "/" + resolved["model"]))
                return
            raise ValueError(f"模型选项缺少可核对来源: {pointer(path)}")

        visit(options, ())
        options["endpoint"] = public_endpoint(options["endpoint"])
        return ConfigModelInspectionDTO(
            model_ref=reference, model=resolved["model"],
            options=redact_source_payload(options, (*provider_path, "defaults")), origins=option_origins,
            request_url=public_endpoint(model_request_url(resolved)),
            catalog_url=None if resolved.get("auth") is not None else public_endpoint(str(model_catalog_url(resolved))),
        )

    @staticmethod
    def _provider_definition(config: Mapping[str, Any], provider_id: str) -> Mapping[str, Any]:
        llm = config.get("llm")
        if not isinstance(llm, Mapping):
            raise TypeError("llm 配置必须是对象")
        providers = llm.get("providers")
        if not isinstance(providers, Mapping):
            raise TypeError("llm.providers 必须是以稳定 Provider ID 为键的对象")
        if provider_id not in providers:
            raise ValueError(f"引用了不存在的 Provider: {provider_id}")
        provider = providers[provider_id]
        if not isinstance(provider, Mapping):
            raise TypeError(f"Provider {provider_id} 必须是对象")
        return provider

    def resolve_provider_defaults(self, config: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
        """目录发现只解析当前连接，不要求已有 Model 或枚举其它配置。"""
        provider = self._provider_definition(config, provider_id)
        defaults = provider.get("defaults", {})
        if not isinstance(defaults, Mapping):
            raise TypeError(f"Provider {provider_id} 的 defaults 必须是对象")
        resolved = self._merge_model_options(defaults, {})
        resolved["provider_id"] = provider_id
        return self._expand_model_credentials(resolved)

    def list_models(self, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        llm = config.get("llm")
        if not isinstance(llm, Mapping):
            raise TypeError("llm 配置必须是对象")
        providers = llm.get("providers")
        if not isinstance(providers, Mapping):
            raise TypeError("llm.providers 必须是以稳定 Provider ID 为键的对象")
        result = []
        for provider_id, provider in providers.items():
            if not isinstance(provider, Mapping):
                raise TypeError(f"Provider {provider_id} 必须是对象")
            models = provider.get("models")
            if not isinstance(models, Mapping):
                raise TypeError(f"Provider {provider_id} 的 models 必须是对象")
            for model_id in models:
                result.append(self.resolve_model_reference(config, ModelReferenceDTO(
                    provider_id=provider_id, model_id=model_id,
                )))
        return result

    def get_model(self, reference: ModelReferenceDTO | Mapping[str, object]) -> dict[str, Any]:
        return self.resolve_model_reference(self._effective_config_provider(), reference)

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
            return expanded
        if api_key.startswith("${") and api_key.endswith("}"):
            var_name = api_key[2:-1]
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ValueError(f"环境变量 {var_name} 未设置")
            expanded["api_key"] = env_value
        return expanded


    def _merge_model_options(
        self, defaults: Mapping[str, Any], overrides: Mapping[str, Any],
        *, model_name: str | None = None,
    ) -> dict[str, Any]:
        """沿当前快照 schema 合并，最后映射目标协议；不复制整个配置。"""
        schema = self._schema_provider()
        merged = merge_json_objects(dict(defaults), dict(overrides), schema={
            "$ref": "#/$defs/modelOptions", "$defs": schema["$defs"],
        })
        merged = apply_model_metadata(merged, model_name)
        protocol_mode = parse_provider_api_mode(merged)
        request_options = merged.get("request_options", {})
        if not isinstance(request_options, dict):
            raise TypeError("request_options 必须是对象")
        if "parameters" in request_options:
            parameters = request_options["parameters"]
            if not isinstance(parameters, Mapping):
                raise TypeError("request_options.parameters 必须是对象")
            request_options["parameters"] = normalize_request_parameters(parameters, protocol=protocol_mode.protocol)
            validate_configured_parameters(request_options["parameters"])
            validate_extra_body(request_options["parameters"], protocol=protocol_mode.protocol)
        if "default_headers" in request_options:
            headers = request_options["default_headers"]
            if not isinstance(headers, Mapping):
                raise TypeError("request_options.default_headers 必须是对象")
            request_options["default_headers"] = normalize_request_headers(headers)
        return merged


__all__ = ["LlmResolution"]
