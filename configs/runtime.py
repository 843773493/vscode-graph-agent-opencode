from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import jsonschema
from referencing.exceptions import Unresolvable

from app.agents.providers.request_parameters import (
    normalize_request_headers,
    normalize_request_parameters,
)
from app.core.config_migration_guard import require_settled_config_migration
from app.core.config_sources import parse_stable_config_file, read_stable_config_file
from app.services.infrastructure.config.policy import (
    gateway_config_policy,
    validate_policy_manifest,
    workspace_config_policy,
)


def read_jsonc_object(path: Path) -> dict[str, object]:
    require_settled_config_migration(path)
    parsed = parse_stable_config_file(read_stable_config_file(path))
    if parsed is None:
        raise FileNotFoundError("配置文件不存在")
    return parsed


def merge_json_objects(
    base: dict[str, object],
    override: dict[str, object],
    *,
    _path: tuple[str, ...] = (),
) -> dict[str, object]:
    """递归合并；逻辑别名按层覆盖，保留胜出来源的原文键名。"""

    model_options = _path[:2] == ("llm", "providers")
    base_names: dict[str, str] = {}
    override_names: dict[str, str] = {}
    if model_options and _path[-2:] == ("request_options", "parameters"):
        base_names = dict(zip(normalize_request_parameters(base, protocol="responses"), base, strict=True))
        override_names = dict(zip(normalize_request_parameters(override, protocol="responses"), override, strict=True))
    elif model_options and _path[-2:] == ("request_options", "default_headers"):
        base_names = dict(zip(normalize_request_headers(base), base, strict=True))
        override_names = dict(zip(normalize_request_headers(override), override, strict=True))

    result = dict(base)
    # 这里只统一比较身份；实际协议字段在选定模型解析后归一，来源仍可定位原 JSONC。
    for canonical, name in override_names.items():
        previous = base_names.get(canonical)
        if previous is not None and previous != name:
            result[name] = result.pop(previous)
    for key, value in override.items():
        base_value = result.get(key)
        atomic_reasoning = model_options and _path[-1:] == ("api_mode",) and key == "supports_reasoning"
        if isinstance(base_value, dict) and isinstance(value, dict) and not atomic_reasoning:
            result[key] = merge_json_objects(base_value, value, _path=(*_path, key))
        else:
            result[key] = value
    return result


def validate_config(
    config: dict[str, object],
    *,
    schema_path: Path,
) -> dict[str, object]:
    return validate_config_payload(config, schema=read_jsonc_object(schema_path))


def _structural_location(document: object, path: Iterable[str | int]) -> str:
    """按零基对象成员序号和数组下标定位，绝不把用户键名当作诊断文本。"""
    location = "<root>"
    for part in path:
        if isinstance(document, dict):
            ordinal = next(index for index, key in enumerate(document) if key == part)
            location += f".property[{ordinal}]"
        else:
            location += f".item[{part}]"
        document = document[part]
    return location


def validate_config_payload(
    config: dict[str, object], *, schema: dict[str, object],
) -> dict[str, object]:
    """运行时唯一校验入口；公开诊断不复制源文，完整细节留在原始异常链。"""
    schema_domain = schema.get("x-boxteam-config-domain")
    if schema_domain == "workspace":
        require_current_workspace_config(config)
    if schema_domain == "gateway":
        features = config.get("features")
        if isinstance(features, dict) and "session_generators" in features:
            raise ValueError("Gateway 生成器轮询配置已迁入工作区；停止服务后执行 uv run python -m configs.boxteam migrate-runtime-config，再通过 /api/gateway/migrations/session-generators 显式导入旧任务")
        logging = config.get("logging")
        runtime = logging.get("runtime") if isinstance(logging, dict) else None
        if isinstance(runtime, dict) and isinstance(runtime.get("level"), str):
            runtime["level"] = runtime["level"].strip().lower()
    try:
        if schema_domain == "workspace":
            validate_policy_manifest(schema, workspace_config_policy())
        elif schema_domain == "gateway":
            validate_policy_manifest(schema, gateway_config_policy())
    except (TypeError, ValueError) as error:
        raise ValueError(f"配置策略验证失败: domain={schema_domain}") from error
    try:
        jsonschema.validate(config, schema)
    except Unresolvable as error:
        # 引用可能是含原生路径或凭据的 URI，不复制到普通配置状态。
        raise ValueError("配置验证失败: stage=schema_reference") from error
    except (jsonschema.ValidationError, jsonschema.SchemaError) as error:
        schema_error = isinstance(error, jsonschema.SchemaError)
        stage = "schema" if schema_error else "config"
        location = _structural_location(schema if schema_error else config, error.absolute_path)
        # validator 是 jsonschema 内置规则名；message/instance/path 可能含秘密和原生路径。
        raise ValueError(
            f"配置验证失败: stage={stage}, location={location}, rule={error.validator}"
        ) from error
    if schema_domain == "workspace":
        from app.services.infrastructure.config.llm_resolution import LlmResolution

        resolver = LlmResolution(
            effective_config_provider=lambda: config,
            default_agent_id_provider=lambda: config["default_agent"],
        )
        validator = jsonschema.Draft202012Validator({
            "$ref": "#/$defs/resolvedModelOptions", "$defs": schema["$defs"],
        })
        option_fields = schema["$defs"]["modelOptions"]["properties"]
        for model in resolver.list_models(config):
            options = {key: value for key, value in model.items() if key in option_fields}
            try:
                validator.validate(options)
            except jsonschema.ValidationError as error:
                raise ValueError(
                    "模型配置验证失败: "
                    f"provider_id={model['provider_id']}, model_id={model['model_id']}, "
                    f"location={_structural_location(options, error.absolute_path)}, rule={error.validator}"
                ) from error
    return config


def require_current_workspace_config(config: dict[str, object], *, partial: bool = False) -> None:
    if "logger" in config or "logging" in config:
        raise ValueError(
            "Workspace 日志配置须显式迁移到 Gateway logging.runtime；停止相关服务后执行 "
            "uv run python -m configs.boxteam migrate-runtime-config --migration-workspace <工作区>"
        )
    if config.get("config_version", 3 if partial else None) != 3:
        raise ValueError("Workspace 配置版本不受支持；当前版本为 3，模型选择须使用完整 ModelReference，原件保持不变")


def load_validated_config(
    *,
    config_path: Path,
    schema_path: Path,
) -> dict[str, object]:
    config = read_jsonc_object(config_path)
    return validate_config(
        config,
        schema_path=schema_path,
    )
