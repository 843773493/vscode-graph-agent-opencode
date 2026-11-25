"""用户授权的 Workspace v3 平面来源转为 v4 节点；普通读取不接受旧格式。"""

from __future__ import annotations

from app.services.infrastructure.config.state import (
    restore_environment_secret_references,
)


def _options_settings(options: dict[str, object]) -> dict[str, object]:
    settings: dict[str, object] = {"model_options": options}
    request = options.get("request_options")
    if isinstance(request, dict) and "no_proxy" in request:
        direct = request.pop("no_proxy")
        if type(direct) is not bool:
            raise TypeError("v3 request_options.no_proxy 必须为布尔值")
        settings["network"] = {"proxy": {"mode": "direct" if direct else "environment"}}
    return settings


def migrate_v3_jsonc_to_v4_jsonc(
    payload: dict[str, object], *, source_key: str,
) -> dict[str, object]:
    if payload.get("config_version") != 3:
        raise ValueError("Workspace v3 转换只接受 config_version=3 的平面来源")
    parents = {
        "workspace.jsonc": "builtin:workspace",
        "workspace_mutable_override": "builtin:workspace",
        "workspace_local.jsonc": "user:workspace",
        "workspace_local_mutable_override": "user:workspace",
        "workspace_root_mutable_override": "user:workspace_local",
    }
    if source_key not in parents:
        raise ValueError(f"Workspace v3 来源层未知，保留原件: {source_key}")
    business = restore_environment_secret_references(
        {key: value for key, value in payload.items() if key not in {"$schema", "config_version"}},
    )
    if "logger" in business or "logging" in business:
        raise ValueError("此授权来源不支持 Workspace 日志取舍；保留原件")
    settings: dict[str, object] = {"workspace": business}
    if "network" in business:
        settings["network"] = business.pop("network")
    profiles: dict[str, object] = {}
    llm = business.get("llm", {})
    for provider_id, provider in llm.get("providers", {}).items():
        defaults = provider.pop("defaults", None)
        if defaults is not None:
            name = f"provider.{provider_id}"
            profiles[name] = {"extends": "context:workspace", "settings": _options_settings(defaults)}
            # JSON Pointer 转义保留稳定 ID，不从显示名生成新身份。
            pointer = name.replace("~", "~0").replace("/", "~1")
            provider["config_ref"] = f"#/profiles/{pointer}"
        for model_id, model in provider.get("models", {}).items():
            overrides = model.pop("overrides", None)
            reference = f"context:provider/{provider_id}"
            if overrides:
                name = f"model.{provider_id}.{model_id}"
                profiles[name] = {"extends": reference, "settings": _options_settings(overrides)}
                pointer = name.replace("~", "~0").replace("/", "~1")
                reference = f"#/profiles/{pointer}"
            model["config_ref"] = reference
    return {
        **({"$schema": payload["$schema"]} if "$schema" in payload else {}),
        "config_version": 4, "extends": parents[source_key], "settings": settings,
        **({"profiles": profiles} if profiles else {}),
    }
