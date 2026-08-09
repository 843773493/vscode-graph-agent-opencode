from __future__ import annotations

from pathlib import Path

import commentjson
import jsonschema


def read_jsonc_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    parsed = commentjson.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError(f"配置文件根节点必须是对象: {path}")
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
    config_path: Path,
    schema_path: Path,
) -> dict[str, object]:
    schema = read_jsonc_object(schema_path)
    try:
        jsonschema.validate(config, schema)
    except jsonschema.ValidationError as error:
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ValueError(
            f"配置验证失败: config={config_path} schema={schema_path} "
            f"location={location}: {error.message}"
        ) from error
    return config


def load_validated_config(
    *,
    config_path: Path,
    schema_path: Path,
) -> dict[str, object]:
    config = read_jsonc_object(config_path)
    return validate_config(
        config,
        config_path=config_path,
        schema_path=schema_path,
    )
