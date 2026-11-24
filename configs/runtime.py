from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

import jsonschema
from referencing.exceptions import Unresolvable

from app.core.config_migration_guard import require_settled_config_migration
from app.core.config_sources import parse_stable_config_file, read_stable_config_file
from app.domain.context_compaction import CompactionPolicy
from app.services.infrastructure.config.policy import (
    gateway_config_policy,
    validate_policy_manifest,
    workspace_config_policy,
)
from configs.inheritance.merge import SettingsMerger
from configs.inheritance.values import FieldOrigin, freeze, thaw


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
    schema: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """配置 owner 提供 schema；值与来源共用节点合并，不按业务路径特判。"""
    merger = SettingsMerger(schema if schema is not None else {})
    value, origins = merger.merge(None, freeze(base), origin=FieldOrigin("base", "", ()))
    value, origins = merger.merge(value, freeze(override), origin=FieldOrigin("override", "", ()),
                                  inherited_origins=origins)
    return cast(dict[str, object], thaw(value, origins=origins))


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


def validate_config_document_payload(
    config: dict[str, object], *, schema: dict[str, object],
) -> dict[str, object]:
    """只校验本地节点文档；可达父链与完整有效值由所属 Graph owner 校验。"""
    if schema.get("x-boxteam-config-domain") == "workspace":
        require_current_workspace_config(config)
    return _validate_config_schema(config, schema=schema)


def validate_config_payload(
    config: dict[str, object], *, schema: dict[str, object],
) -> dict[str, object]:
    """只校验业务 API 的 flat 投影，不从投影重建配置图或模型解析器。"""
    domain = schema.get("x-boxteam-config-domain")
    if domain == "workspace":
        require_current_workspace_config(config)
    if domain == "gateway":
        features = config.get("features")
        if isinstance(features, dict) and "session_generators" in features:
            raise ValueError("Gateway 生成器轮询配置已迁入工作区；停止服务后执行 uv run python -m configs.boxteam migrate-runtime-config，再通过 /api/gateway/migrations/session-generators 显式导入旧任务")
        logging = config.get("logging")
        runtime = logging.get("runtime") if isinstance(logging, dict) else None
        if isinstance(runtime, dict) and isinstance(runtime.get("level"), str):
            runtime["level"] = runtime["level"].strip().lower()
    definition = f"{domain}Payload" if domain in {"gateway", "workspace"} else None
    _validate_config_schema(config, schema=schema, definition=definition)
    if domain == "workspace":
        CompactionPolicy.model_validate(config.get("context", {}).get("compaction", {}))
    return config


def _validate_config_schema(
    config: dict[str, object], *, schema: dict[str, object], definition: str | None = None,
) -> dict[str, object]:
    """公开诊断不复制源文，完整细节留在原始异常链。"""
    schema_domain = schema.get("x-boxteam-config-domain")
    try:
        if schema_domain == "workspace":
            validate_policy_manifest(schema, workspace_config_policy())
        elif schema_domain == "gateway":
            validate_policy_manifest(schema, gateway_config_policy())
    except (TypeError, ValueError) as error:
        raise ValueError(f"配置策略验证失败: domain={schema_domain}") from error
    try:
        value_schema = ({"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}
                        if definition is not None else schema)
        jsonschema.validate(config, value_schema)
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
    if "logger" in config or "logging" in config:
        raise ValueError("Workspace 不允许日志配置；旧来源缺少已验收升级基线，原件保持不变")
    if config.get("config_version", 4 if partial else None) != 4:
        raise ValueError("Workspace 配置版本不受支持；当前版本为 4，旧来源缺少已验收升级基线，原件保持不变")
