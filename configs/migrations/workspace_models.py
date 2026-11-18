"""Workspace v2→v3：模型目录与 Agent 引用一次性转为稳定 ID。"""

from __future__ import annotations

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.providers.request_parameters import formal_request_parameter_names


def migrate_workspace_models(payload: dict[str, object]) -> None:
    """仅修改维护入口持有的候选；当前版本不接受旧结构回退。"""
    if payload.get("config_version", 1) not in (1, 2):
        return
    llm = payload.get("llm")
    providers = llm.get("providers") if isinstance(llm, dict) else None
    if isinstance(providers, list):
        catalog: dict[str, object] = {}
        for provider in providers:
            if not isinstance(provider, dict):
                raise TypeError("旧 llm.providers 条目必须是对象")
            provider_id = provider.pop("id", None)
            if not isinstance(provider_id, str) or not provider_id:
                raise ValueError("旧 llm.providers 条目缺少有效 id")
            if provider_id in catalog:
                raise ValueError(f"旧 llm.providers 包含重复 id: {provider_id}")
            if "defaults" in provider or "models" in provider:
                raise ValueError(f"旧 Provider 混用新旧模型结构: {provider_id}")
            model = provider.pop("model", None)
            if not isinstance(model, str) or not model:
                raise ValueError(f"旧 Provider 缺少有效 model: {provider_id}")
            options = provider.get("request_options")
            if isinstance(options, dict) and "overrides" in options:
                if "parameters" in options:
                    raise ValueError(f"Provider 同时声明 overrides 和 parameters: {provider_id}")
                options["parameters"] = options.pop("overrides")
                parameters = options["parameters"]
                extra = parameters.get("extra_body") if isinstance(parameters, dict) else None
                if isinstance(extra, dict):
                    formal = formal_request_parameter_names(parse_provider_api_mode(provider).protocol)
                    for key in sorted(extra.keys() & formal):
                        if key in parameters:
                            raise ValueError(f"Provider 正式参数与 extra_body 重复: {provider_id}.{key}")
                        parameters[key] = extra.pop(key)
            catalog[provider_id] = {"defaults": provider, "models": {"default": {"model": model}}}
        llm["providers"] = catalog
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        return
    for agent_id, agent in agents.items():
        model = agent.get("model") if isinstance(agent, dict) else None
        if not isinstance(model, dict):
            continue
        for old, new, multiple in (("primary_provider", "primary", False), ("fallback_providers", "fallbacks", True)):
            if old not in model:
                continue
            if new in model:
                raise ValueError(f"Agent 同时声明 {old} 和 {new}: {agent_id}")
            value = model.pop(old)
            if multiple:
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f"Agent {old} 必须是 Provider ID 数组: {agent_id}")
                model[new] = [{"provider_id": item, "model_id": "default"} for item in value]
            else:
                if not isinstance(value, str):
                    raise ValueError(f"Agent {old} 必须是 Provider ID: {agent_id}")
                model[new] = {"provider_id": value, "model_id": "default"}
