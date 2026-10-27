from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import jsonschema
from referencing.exceptions import Unresolvable

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
) -> dict[str, object]:
    """递归合并对象；标量和数组由高优先级配置整体覆盖。"""

    result = dict(base)
    for key, value in override.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = merge_json_objects(base_value, value)
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
            raise ValueError("Gateway 生成器轮询配置已迁入工作区；停止服务后执行 uv run python -m configs.boxteam migrate-runtime-logging，再通过 /api/gateway/migrations/session-generators 显式导入旧任务")
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
    return config


def require_current_workspace_config(config: dict[str, object], *, partial: bool = False) -> None:
    if "logger" in config or "logging" in config or config.get("config_version", 2 if partial else None) != 2:
        raise ValueError(
            "Workspace 日志配置须显式迁移到 Gateway logging.runtime；停止相关服务后执行 "
            "uv run python -m configs.boxteam migrate-runtime-logging --migration-workspace <工作区>"
        )


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
