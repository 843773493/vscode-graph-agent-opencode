"""配置 owner 持有的插件私有输入；普通配置只保存精确不可变引用。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import cast
from uuid import uuid4

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError
from pydantic import JsonValue, Secret
from pydantic import ValidationError as ModelValidationError

from app.core.atomic_fs import atomic_write_bytes, fsync_directory, native_io_path
from app.core.file_permissions import (
    protect_readonly_file,
    require_private_file_access,
    restrict_private_path_access,
)
from configs.plugin_parameters import PluginPrivateInputReference

_ENV_REFERENCE = re.compile(r"^(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|env:([A-Za-z_][A-Za-z0-9_]*))$")
_PRIVATE_ONE_OF_AMBIGUITY = "插件私有输入的 oneOf 分支不能唯一确定"
_PRIVATE_ONE_OF_MARKER = "private-input-oneOf-ambiguous"


def _environment_name(value: object) -> str | None:
    match = _ENV_REFERENCE.fullmatch(value) if isinstance(value, str) else None
    return (match[1] or match[2]) if match else None


def _is_reference(value: object) -> bool:
    return isinstance(value, dict) and value.get("kind") == "plugin_private_file"


def private_input_references(value: object) -> tuple[PluginPrivateInputReference, ...]:
    """从来源或恢复快照中提取正式插件私有引用，不解析秘密正文。"""
    if _is_reference(value):
        return (PluginPrivateInputReference.model_validate(value),)
    if isinstance(value, Mapping):
        return tuple(reference for child in value.values() for reference in private_input_references(child))
    if isinstance(value, list):
        return tuple(reference for child in value for reference in private_input_references(child))
    return ()


def remove_unreferenced_plugin_private_inputs(
    directory: Path,
    *,
    candidates: Iterable[PluginPrivateInputReference],
    retained: Iterable[PluginPrivateInputReference],
) -> tuple[str, ...]:
    """回收配置 owner 本次释放、且所有当前快照均不再引用的不可变值。"""
    directory = native_io_path(directory)
    retained_versions = {
        (reference.gateway_id, reference.workspace_id, reference.version)
        for reference in retained
    }
    removed: list[str] = []
    for reference in candidates:
        identity = reference.gateway_id, reference.workspace_id, reference.version
        if identity in retained_versions:
            continue
        path = directory / f"{reference.version}.json"
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("插件私有输入清理目标不是普通文件")
        path.chmod(0o600)
        path.unlink()
        removed.append(reference.version)
    if removed:
        fsync_directory(directory)
    return tuple(removed)


def has_parameter_references(value: object) -> bool:
    """仅判断是否需要取得 schema；不据此认定字段是秘密或直接解引用。"""
    if _is_reference(value) or _environment_name(value):
        return True
    if isinstance(value, Mapping):
        return any(has_parameter_references(child) for child in value.values())
    if isinstance(value, list):
        return any(has_parameter_references(child) for child in value)
    return False


def _secret_field(_validator, write_only, _value, _schema) -> Iterator[ValidationError]:
    if write_only:
        yield ValidationError("private-parameter")


def _secret_errors(errors: Iterator[ValidationError]) -> Iterator[ValidationError]:
    for error in errors:
        if error.validator == "writeOnly" or error.validator_value == _PRIVATE_ONE_OF_MARKER:
            yield error
        yield from _secret_errors(iter(error.context))


def requires_private_delivery(values: dict[str, JsonValue], schema: Mapping[str, object] | None) -> bool:
    errors = list(_secret_errors(_SecretFields(schema or {}).iter_errors(values)))
    if any(error.validator_value == _PRIVATE_ONE_OF_MARKER for error in errors):
        raise ValueError(_PRIVATE_ONE_OF_AMBIGUITY)
    return any(_is_reference(error.instance) or _environment_name(error.instance) for error in errors)


def _source_constraint(name: str, validate: Callable | None) -> Callable:
    def check(validator, constraint, instance, schema) -> Iterator[ValidationError]:
        if name == "writeOnly":
            if constraint and not (_is_reference(instance) or _environment_name(instance)):
                yield ValidationError("writeOnly 参数必须使用插件私有文件引用")
        elif not (schema.get("writeOnly") and (_is_reference(instance) or _environment_name(instance))) and validate is not None:
            yield from validate(validator, constraint, instance, schema)
    return check


# 引用在来源中代替实际值，实际值仍由原版 JSON Schema 完整校验。
_SourceValidator = validators.extend(Draft202012Validator, {
    **{name: _source_constraint(name, validate) for name, validate in Draft202012Validator.VALIDATORS.items()},
    "writeOnly": _source_constraint("writeOnly", None),
})


def _secret_one_of(validator, choices, instance, _schema) -> Iterator[ValidationError]:
    source_validator = _SourceValidator(validator.schema, _resolver=validator._resolver)
    matching = [index for index, choice in enumerate(choices)
                if source_validator.evolve(schema=choice).is_valid(instance)]
    if len(matching) != 1:
        yield ValidationError(
            _PRIVATE_ONE_OF_AMBIGUITY,
            validator="oneOf",
            validator_value=_PRIVATE_ONE_OF_MARKER,
        )
        return
    yield from validator.descend(instance, choices[matching[0]], schema_path=matching[0])


_SecretFields = validators.extend(_SourceValidator, {
    "writeOnly": _secret_field,
    "oneOf": _secret_one_of,
})


def _private_errors(schema: Mapping[str, object] | None, values: dict[str, JsonValue]) -> list[ValidationError]:
    if schema is None:
        return []
    errors = list(_secret_errors(_SecretFields(schema).iter_errors(values)))
    if any(error.validator_value == _PRIVATE_ONE_OF_MARKER for error in errors):
        raise ValueError(_PRIVATE_ONE_OF_AMBIGUITY)
    return errors


def validate_plugin_parameters(schema: Mapping[str, object] | None, source: dict[str, JsonValue],
                               resolved: dict[str, JsonValue] | None = None) -> None:
    if schema is None:
        if source:
            raise ValueError("插件未声明参数")
        return
    checks = [(_SourceValidator(schema), source)]
    if resolved is not None:
        checks.append((Draft202012Validator(schema), resolved))
    for validator, value in checks:
        error = next(validator.iter_errors(value), None)
        if error is not None:
            # 不复制 jsonschema 的 message/instance/schema，避免错误链携带秘密正文。
            raise ValueError(f"插件参数不符合 schema: constraint={error.validator}")


def validate_private_input(schema: Mapping[str, object] | None, source: dict[str, JsonValue],
                           pointer: str, value: JsonValue) -> None:
    """沿 JSON Schema 自身的遍历定位 writeOnly，复用其 $ref／组合／数组规则。"""
    if schema is None:
        raise ValueError("插件未声明参数 schema")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    candidate = deepcopy(source)
    current: JsonValue = candidate
    location: list[str | int] = []
    for index, part in enumerate(parts):
        if isinstance(current, dict):
            location.append(part)
            if index == len(parts) - 1:
                current[part] = {"kind": "plugin_private_file"}
            else:
                current = current.setdefault(part, {})
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(current):
            location.append(int(part))
            if index == len(parts) - 1:
                current[int(part)] = {"kind": "plugin_private_file"}
            else:
                current = current[int(part)]
        else:
            raise ValueError("插件私有参数位置不是可写的对象字段或已有数组项")
    declared = [error for error in _private_errors(schema, candidate) if list(error.absolute_path) == location]
    if not declared:
        raise ValueError("参数位置不是此包版本声明的 writeOnly 字段")
    for field in declared:
        error = next(Draft202012Validator(schema).evolve(schema=field.schema).iter_errors(value), None)
        if error is not None:
            raise ValueError(f"插件私有输入不符合 schema: constraint={error.validator}")


class PluginPrivateInputStore:
    def __init__(self, *, directory: Path, gateway_id: str, workspace_id: str | None) -> None:
        self.directory = native_io_path(directory)
        self.gateway_id, self.workspace_id = gateway_id, workspace_id

    def write(self, *, package_id: str, plugin_id: str, parameter_pointer: str,
              value: Secret[JsonValue]) -> PluginPrivateInputReference:
        reference = PluginPrivateInputReference(gateway_id=self.gateway_id, workspace_id=self.workspace_id,
            package_id=package_id, plugin_id=plugin_id, parameter_pointer=parameter_pointer, version=uuid4().hex)
        self.directory.mkdir(parents=True, exist_ok=True)
        restrict_private_path_access(self.directory, is_directory=True)
        path = self.directory / f"{reference.version}.json"
        if path.exists():
            raise FileExistsError("插件私有输入版本已存在，不能覆盖")
        atomic_write_bytes(path, json.dumps({"reference": reference.model_dump(mode="json"),
            "value": value.get_secret_value()}, ensure_ascii=False).encode())
        restrict_private_path_access(path)
        protect_readonly_file(path)
        return reference

    def remove_unreferenced(self, *, candidates: tuple[PluginPrivateInputReference, ...],
                            retained: tuple[PluginPrivateInputReference, ...]) -> tuple[str, ...]:
        """只删除本次配置变更明确释放、且来源/active/pending 都不再引用的版本。"""
        owner = self.gateway_id, self.workspace_id
        return remove_unreferenced_plugin_private_inputs(
            self.directory,
            candidates=tuple(reference for reference in candidates
                             if (reference.gateway_id, reference.workspace_id) == owner),
            retained=tuple(reference for reference in retained
                           if (reference.gateway_id, reference.workspace_id) == owner),
        )

    def resolve(self, values: dict[str, JsonValue], *, package_id: str, plugin_id: str,
                schema: Mapping[str, object] | None) -> dict[str, JsonValue]:
        secret_paths = {tuple(map(str, error.absolute_path))
                        for error in _private_errors(schema, values)}
        def visit(value: JsonValue, pointer: str) -> JsonValue:
            name = _environment_name(value)
            parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:])
            if name is not None and parts in secret_paths:
                if name not in os.environ:
                    raise ValueError(f"插件私有参数的目标环境变量未配置: {name}")
                return os.environ[name]
            if _is_reference(value) and parts in secret_paths:
                try:
                    reference = PluginPrivateInputReference.model_validate(value)
                except ModelValidationError:
                    raise ValueError("插件私有引用结构无效") from None
                if (reference.gateway_id, reference.workspace_id, reference.package_id,
                    reference.plugin_id, reference.parameter_pointer) != (
                        self.gateway_id, self.workspace_id, package_id, plugin_id, pointer):
                    raise ValueError("插件私有引用不属于当前目标、插件或参数位置")
                path = self.directory / f"{reference.version}.json"
                try:
                    with path.open("rb") as stream:
                        require_private_file_access(path, descriptor=stream.fileno())
                        saved = json.load(stream)
                except FileNotFoundError:
                    raise ValueError(f"插件私有输入版本不存在: {reference.version}") from None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ValueError("插件私有输入文件 JSON 无效") from None
                if not isinstance(saved, dict):
                    raise ValueError("插件私有输入文件结构无效")
                if saved.get("reference") != reference.model_dump(mode="json") or "value" not in saved:
                    raise ValueError("插件私有输入文件与引用身份不一致")
                return cast(JsonValue, saved["value"])
            if isinstance(value, dict):
                return {key: visit(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
                        for key, child in value.items()}
            if isinstance(value, list):
                return [visit(child, pointer + f"/{index}") for index, child in enumerate(value)]
            return value
        return cast(dict[str, JsonValue], visit(values, ""))
