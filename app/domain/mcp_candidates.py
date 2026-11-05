"""MCP 来源、确切 variant 与参数的确定性候选规则；不读取网络或运行资源。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Literal, cast

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs

VariantKind = Literal["package", "remote", "manual"]
_INPUT_FIELDS = frozenset({"description", "isRequired", "format", "value", "isSecret", "default", "choices"})
_VARIABLE_FIELDS = _INPUT_FIELDS | {"variables"}
_ARGUMENT_FIELDS = _VARIABLE_FIELDS | {"type", "name", "valueHint", "isRepeated"}
_PACKAGE_FIELDS = frozenset({"registryType", "identifier", "version", "registryBaseUrl", "fileSha256", "transport",
                              "packageArguments", "runtimeHint", "runtimeArguments", "environmentVariables"})
_REMOTE_FIELDS = frozenset({"type", "url", "headers", "variables"})
_MANIFEST_FIELDS = frozenset({"$schema", "name", "version", "description", "title", "packages", "remotes", "repository",
                             "websiteUrl", "website_url", "icons", "_meta", "status", "created_at", "updated_at"})
_V0_FIELDS = {"registry_type": "registryType", "registry_name": "registryType", "name": "identifier",
              "registry_base_url": "registryBaseUrl", "file_sha256": "fileSha256", "package_arguments": "packageArguments",
              "runtime_hint": "runtimeHint", "runtime_arguments": "runtimeArguments", "environment_variables": "environmentVariables",
              "is_required": "isRequired", "is_secret": "isSecret", "value_hint": "valueHint", "is_repeated": "isRepeated"}
_VARIABLE_REFERENCE = re.compile(r"(?<!\$)\{([a-zA-Z_][a-zA-Z0-9_.-]*)\}")


class McpCandidateConflict(ValueError):
    """冻结的目标、来源或参数基线已变化，必须重新选择。"""


@dataclass(frozen=True, slots=True)
class McpCandidateVariant:
    kind: VariantKind
    index: int
    payload: bytes

    @property
    def digest(self) -> str:
        return sha256_jcs({"kind": self.kind, "payload": json.loads(self.payload)})

    def ref(self) -> dict[str, object]:
        return {"kind": self.kind, "index": self.index, "digest": self.digest}

    def describe(self) -> dict[str, object]:
        value = _object(json.loads(self.payload), "variant")
        transport = value.get("transport") if self.kind == "manual" else (
            value.get("type") if self.kind == "remote" else _object(value.get("transport"), "package.transport").get("type"))
        return {"ref": self.ref(), "transport": _text(transport, "variant.transport"),
                "registry_type": value.get("registryType"), "identifier": value.get("identifier"), "version": value.get("version")}


@dataclass(frozen=True, slots=True)
class McpCandidateManifest:
    digest: str
    entry_id: str | None
    variants: tuple[McpCandidateVariant, ...]

    def select(self, manifest_digest: str, kind: str, index: int, digest: str) -> McpCandidateVariant:
        if manifest_digest != self.digest:
            raise McpCandidateConflict("MCP manifest 基线已改变，请重新读取来源并选择 variant")
        for variant in self.variants:
            if (variant.kind, variant.index, variant.digest) == (kind, index, digest):
                return variant
        raise McpCandidateConflict("所选 MCP variant 已消失或改变，不能改用其它候选")


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} 必须是对象")
    return cast(dict[str, object], value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def _fields(value: dict[str, object], allowed: frozenset[str], field: str) -> None:
    if set(value) - allowed:
        raise ValueError(f"{field} 包含不能无损映射的字段")


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{field} 必须是数组")
    return value


def _legacy_object(value: object, *, package: bool = False) -> dict[str, object]:
    """只在显式 v0 的旧 server 形状内转换官方字段，不探测另一协议。"""
    result: dict[str, object] = {}
    for key, child in _object(value, "v0 definition").items():
        renamed = _V0_FIELDS.get(key, key) if package or key != "name" else key
        if renamed == "registryType" and child in {"docker", "docker-hub"}:
            child = "oci"
        if renamed in result and result[renamed] != child:
            raise ValueError("v0 MCP definition 包含冲突字段")
        result[renamed] = child
    for key in ("headers", "environmentVariables", "packageArguments", "runtimeArguments"):
        if key in result:
            result[key] = [_legacy_object(item) for item in _list(result[key], key)]
    if "variables" in result:
        result["variables"] = {key: _legacy_object(item) for key, item in _object(result["variables"], "variables").items()}
    if "transport" in result:
        result["transport"] = _legacy_object(result["transport"])
    if package and "transport" not in result:
        result["transport"] = {"type": "stdio"}
    if result.get("registryType") in {"docker", "docker-hub"}:
        result["registryType"] = "oci"
    return result


def parse_candidate_manifest(
    raw: object, *, kind: str, protocol: str | None, name: str, version: str, entry_id: str | None,
) -> McpCandidateManifest:
    original = _object(raw, "MCP manifest")
    legacy = kind == "gallery" and protocol == "v0" and "server" not in original
    server = _object(original.get("server"), "MCP server") if kind == "gallery" and not legacy else original
    if kind == "gallery" and not legacy:
        _fields(original, frozenset({"server", "_meta"}), "MCP registry envelope")
    if (server.get("name"), server.get("version")) != (name, version):
        raise McpCandidateConflict("MCP 来源条目的名称或精确版本已改变")
    # Registry 的 official metadata 属于原始 envelope；server._meta 由发布者提供。
    metadata = _object(original.get("_meta", {}), "MCP metadata") if kind == "gallery" else {}
    official = _object(metadata.get("io.modelcontextprotocol.registry/official", {}), "MCP entry metadata")
    actual_id = official.get("id")
    if actual_id is not None:
        actual_id = _text(actual_id, "MCP entry ID")
    if entry_id is not None and entry_id != actual_id:
        raise McpCandidateConflict("MCP 来源条目身份已改变")
    variants: list[McpCandidateVariant] = []
    if kind == "manual" and "definition" in server:
        value = _object(server["definition"], "manual definition")
        if "transport" in value:
            variants.append(McpCandidateVariant("manual", 0, canonical_json_bytes(value)))
        else:
            _fields(value, frozenset({"packages", "remotes"}), "manual manifest")
            server = value
    else:
        _fields(server, _MANIFEST_FIELDS, "MCP manifest")
    for field, variant_kind in (("packages", "package"), ("remotes", "remote")):
        for index, raw_variant in enumerate(_list(server.get(field, []), field)):
            payload = _legacy_object(raw_variant, package=field == "packages") if legacy else _object(raw_variant, field)
            variants.append(McpCandidateVariant(cast(VariantKind, variant_kind), index, canonical_json_bytes(payload)))
    if not variants:
        raise ValueError("MCP manifest 没有明确 package、remote 或手动配置")
    return McpCandidateManifest(sha256_jcs(original), cast(str | None, actual_id), tuple(variants))


class _Inputs:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.used: set[str] = set()
        self.definitions: dict[str, str] = {}

    def value(self, definition: dict[str, object], key: str, *, repeated: bool = False) -> list[str]:
        _fields(definition, _INPUT_FIELDS, "MCP input")
        for flag in ("isRequired", "isSecret"):
            if flag in definition and not isinstance(definition[flag], bool):
                raise ValueError(f"MCP input.{flag} 必须是布尔值")
        if definition.get("isSecret") is True:
            # TODO(3.2)：secret arguments、模板变量和非 env 输入尚未实现私有引用消费。
            raise ValueError("MCP secret input 尚无工作区私有引用能力，不能创建此候选")
        choices = definition.get("choices")
        if choices is not None and (not isinstance(choices, list) or not all(isinstance(item, str) for item in choices)):
            raise ValueError("MCP input.choices 必须是字符串数组")
        format_name = definition.get("format", "string")
        if format_name not in {"string", "number", "boolean", "filepath"}:
            raise ValueError("MCP input.format 不受支持")
        if "value" not in definition:
            signature = sha256_jcs(definition)
            if key in self.definitions and self.definitions[key] != signature:
                raise ValueError("MCP input 名称存在冲突定义")
            self.definitions[key] = signature
        if "value" in definition:
            selected = definition["value"]
        elif key in self.values:
            self.used.add(key)
            selected = self.values[key]
        elif "default" in definition:
            selected = definition["default"]
        elif definition.get("isRequired"):
            raise ValueError("MCP 候选缺少必需 input")
        else:
            return []
        selected_values = _list(selected, "repeated input") if isinstance(selected, list) and repeated else [selected]
        if not selected_values and definition.get("isRequired"):
            raise ValueError("MCP repeated input 不能为空")
        for item in selected_values:
            if not isinstance(item, str):
                raise TypeError("MCP input 必须是字符串，重复参数才允许字符串数组")
            if definition.get("isRequired") and not item:
                raise ValueError("MCP 必需 input 不能为空字符串")
            if choices is not None and item not in choices:
                raise ValueError("MCP input 不在允许的 choices 中")
            if format_name == "boolean" and item not in {"true", "false"}:
                raise ValueError("MCP boolean input 必须为 true 或 false")
            if format_name == "number":
                try:
                    number = float(item)
                except ValueError as error:
                    raise ValueError("MCP number input 无效") from error
                if not math.isfinite(number):
                    raise ValueError("MCP number input 必须有限")
        return cast(list[str], selected_values)

    def template(self, value: str, variables: object) -> str:
        definitions = _object(variables, "MCP variables")
        resolved: dict[str, str] = {}
        for key, definition in definitions.items():
            selected = self.value(_object(definition, "MCP variable"), key)
            if not selected:
                raise ValueError("MCP 模板变量缺少明确值")
            resolved[key] = selected[0]

        def replace(match: re.Match[str]) -> str:
            if match.group(1) not in resolved:
                raise ValueError("MCP 模板引用了未声明的变量")
            return resolved[match.group(1)]

        return _VARIABLE_REFERENCE.sub(replace, value)

    def finish(self) -> None:
        if set(self.values) - self.used:
            raise ValueError("MCP 候选包含未使用或不允许覆盖的 input")


def _resolve_values(definition: dict[str, object], key: str, inputs: _Inputs, *, repeated: bool = False) -> list[str]:
    if "variables" not in definition:
        return inputs.value({key: value for key, value in definition.items() if key in _INPUT_FIELDS}, key, repeated=repeated)
    if definition.get("isSecret") is True:
        raise ValueError("MCP secret input 尚无工作区私有引用能力，不能创建此候选")
    template = definition.get("value", definition.get("default"))
    if not isinstance(template, str):
        raise TypeError("含 variables 的 MCP input 必须提供明确字符串模板")
    resolved = inputs.template(template, definition["variables"])
    return inputs.value({**{field: value for field, value in definition.items() if field in _INPUT_FIELDS}, "value": resolved}, key)


def _arguments(value: object, inputs: _Inputs) -> list[str]:
    result: list[str] = []
    for item in _list(value, "MCP arguments"):
        argument = _object(item, "MCP argument")
        _fields(argument, _ARGUMENT_FIELDS, "MCP argument")
        repeated = argument.get("isRepeated", False)
        if not isinstance(repeated, bool):
            raise TypeError("MCP argument.isRepeated 必须是布尔值")
        argument_type = argument.get("type")
        if argument_type not in {"named", "positional"}:
            raise ValueError("MCP argument.type 不受支持")
        if (argument_type == "named" and "valueHint" in argument) or (argument_type == "positional" and "name" in argument):
            raise ValueError("MCP 参数的 name/valueHint 与 type 不一致")
        key = argument.get("name") if argument_type == "named" else argument.get("valueHint")
        if not isinstance(key, str) or not key:
            if argument_type == "named" or "value" not in argument:
                raise ValueError("MCP 参数缺少明确 name 或 valueHint")
            key = "fixed-positional"
        if argument_type == "named" and set(argument) <= {"type", "name", "description"}:
            result.append(key)
            continue
        for selected in _resolve_values(argument, key, inputs, repeated=repeated):
            if argument_type == "named":
                result.append(key)
            result.append(selected)
    return result


def _private_env_definition(definition: dict[str, object]) -> None:
    if "isRequired" in definition and not isinstance(definition["isRequired"], bool):
        raise ValueError("MCP input.isRequired 必须是布尔值")
    if definition.get("isSecret") is not True or definition.get("format", "string") != "string":
        raise ValueError("MCP 私有 env 必须是明确的 secret string input")
    if any(field in definition for field in ("variables", "value", "default", "choices")):
        raise ValueError("MCP secret env 不支持固定值、默认值、choices 或模板拼接，必须直接引用私有输入")


def secret_env_input_keys(variant: McpCandidateVariant) -> frozenset[str]:
    """写入前核实际声明；不读取输入值，不把其它 secret 映射为 env。"""
    if variant.kind != "package":
        raise ValueError("MCP 私有 env 仅支持 stdio package 的 environmentVariables")
    payload = _object(json.loads(variant.payload), "MCP variant")
    if _object(payload.get("transport"), "MCP package.transport").get("type") != "stdio":
        raise ValueError("MCP 私有 env 仅支持 stdio package")
    keys = set()
    for item in _list(payload.get("environmentVariables", []), "MCP environmentVariables"):
        definition = _object(item, "MCP env input")
        _fields(definition, _VARIABLE_FIELDS | {"name"}, "MCP env input")
        if definition.get("isSecret") is True:
            _private_env_definition(definition)
            keys.add(_text(definition.get("name"), "MCP input.name"))
    return frozenset(keys)


def _key_values(value: object, inputs: _Inputs, *, private_env: bool = False) -> dict[str, object]:
    result: dict[str, object] = {}
    names: set[str] = set()
    for item in _list(value, "MCP key/value inputs"):
        definition = _object(item, "MCP key/value input")
        _fields(definition, _VARIABLE_FIELDS | {"name"}, "MCP key/value input")
        key = _text(definition.get("name"), "MCP input.name")
        if key in names:
            raise ValueError("MCP key/value input 名称重复")
        names.add(key)
        if private_env and definition.get("isSecret") is True:
            _private_env_definition(definition)
            if key not in inputs.values:
                if definition.get("isRequired"):
                    raise ValueError("MCP 候选缺少必需私有 env 引用")
                continue
            reference = _object(inputs.values[key], "MCP 私有 env 引用")
            if reference.get("kind") != "mcp_private_env":
                raise ValueError("MCP secret env 必须使用工作区私有引用，不能提交明文")
            inputs.used.add(key)
            result[key] = reference
            continue
        selected = _resolve_values(definition, key, inputs)
        if selected:
            result[key] = selected[0]
    return result


def candidate_parameters(variant: McpCandidateVariant, values: dict[str, object]) -> tuple[str, dict[str, object]]:
    payload = _object(json.loads(variant.payload), "MCP variant")
    inputs = _Inputs(values)
    if variant.kind == "manual":
        inputs.finish()
        return ("explicit_command" if payload.get("transport") == "stdio" else "configuration_only"), payload
    if variant.kind == "remote":
        _fields(payload, _REMOTE_FIELDS, "MCP remote")
        if payload.get("type") != "streamable-http":
            raise ValueError("MCP remote 仅支持 Streamable HTTP，旧 SSE 明确不支持")
        url = inputs.template(_text(payload.get("url"), "MCP remote.url"), payload.get("variables", {}))
        parameters: dict[str, object] = {"transport": "streamable_http", "url": url,
                                         "headers": _key_values(payload.get("headers", []), inputs)}
        inputs.finish()
        return "configuration_only", parameters
    _fields(payload, _PACKAGE_FIELDS, "MCP package")
    registry = payload.get("registryType")
    if registry not in {"npm", "pypi", "oci", "nuget"}:
        raise ValueError("MCP package registryType 不受支持")
    transport = _object(payload.get("transport"), "MCP package.transport")
    _fields(transport, frozenset({"type"}), "MCP package.transport")
    if transport.get("type") != "stdio":
        raise ValueError("MCP package 目前仅支持 stdio；不能把 SSE 或包内 HTTP 服务改成 remote")
    identifier = _text(payload.get("identifier"), "MCP package.identifier")
    version = _text(payload.get("version"), "MCP package.version")
    if version.lower() in {"latest", "next", "*"} or re.search(r"[\s*^~<>=|,]", version) or (registry != "oci" and not version[0].isdigit()):
        raise ValueError("MCP package 必须选择精确版本，不能使用范围或动态标签")
    expected_sha = payload.get("fileSha256")
    if expected_sha is not None and (not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha) is None):
        raise ValueError("MCP package.fileSha256 必须是 SHA-256 摘要")
    for field in ("registryBaseUrl", "runtimeHint"):
        if field in payload:
            _text(payload[field], f"MCP package.{field}")
    parameters = {"transport": "stdio", "registry_type": registry, "identifier": identifier,
                  "version": version, "registry_base_url": payload.get("registryBaseUrl"),
                  "expected_sha256": expected_sha, "runtime_hint": payload.get("runtimeHint"),
                  "runtime_arguments": _arguments(payload.get("runtimeArguments", []), inputs),
                  "arguments": _arguments(payload.get("packageArguments", []), inputs),
                  "env": _key_values(payload.get("environmentVariables", []), inputs, private_env=True)}
    inputs.finish()
    return "requires_artifact", parameters
