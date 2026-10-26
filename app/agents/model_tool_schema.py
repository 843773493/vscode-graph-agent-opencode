from __future__ import annotations

import json
from typing import Any, cast

from jsonschema import Draft202012Validator, validators
from jsonschema.protocols import Validator
from langchain_core.tools import BaseTool
from pydantic import BaseModel
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from app.domain.itemized.hashing import canonical_json_bytes


def model_tool_schema_validator(schema: dict[str, Any]) -> Validator:
    """校验公开契约及本地引用；禁止隐式联网或未知方言降级。"""
    dialect = schema.get("$schema", Draft202012Validator.META_SCHEMA["$id"])
    if not isinstance(dialect, str):
        raise TypeError("工具 JSON Schema 的 $schema 必须是 URI 字符串")
    # 使用库已登记的标准方言，不让 validator_for 对未知 URI 警告后降级。
    validator_type = validators.validator_for({"$schema": dialect}, default=Draft202012Validator)
    if dialect.rstrip("#") != validator_type.META_SCHEMA.get("$id", validator_type.META_SCHEMA.get("id", "")).rstrip("#"):
        raise ValueError(f"工具 JSON Schema 方言不受支持: {schema.get('$schema')!r}")
    validator_type.check_schema(schema)
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry = Registry().with_resource("", resource)
    pending = [(resource, registry.resolver().in_subresource(resource))]
    while pending:
        current, resolver = pending.pop()
        if isinstance(current.contents, dict):
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                reference = current.contents.get(keyword)
                if reference is None:
                    continue
                if not isinstance(reference, str) or (reference and not reference.startswith("#")):
                    raise ValueError(f"工具 JSON Schema 只支持本地引用: {keyword}={reference!r}")
                try:
                    resolver.lookup(reference)
                except Unresolvable as error:
                    raise ValueError(f"工具 JSON Schema 本地引用无法解析: {reference!r}") from error
        pending.extend((child, resolver.in_subresource(child)) for child in current.subresources())
    return validator_type(schema, registry=registry)


def get_model_tool_schema(tool: BaseTool) -> type[BaseModel]:
    """返回 LangChain 面向模型公开的权威 Pydantic 工具参数模型。"""
    tool_call_schema = getattr(tool, "tool_call_schema", None)
    if not isinstance(tool_call_schema, type) or not issubclass(
        tool_call_schema,
        BaseModel,
    ):
        raise TypeError(
            "工具缺少模型可见的 Pydantic tool_call_schema: "
            f"tool_name={tool.name} tool_type={type(tool).__name__}"
        )
    return tool_call_schema


def validate_model_tool_arguments(
    tool: BaseTool,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """按公开契约校验，保留字典参数及本地模型既有转换和额外字段策略。"""
    if not isinstance(arguments, dict):
        raise TypeError(
            "模型工具参数必须是 object: "
            f"tool_name={tool.name} actual_type={type(arguments).__name__}"
        )
    tool_call_schema = getattr(tool, "tool_call_schema", None)
    if isinstance(tool_call_schema, dict):
        model_tool_schema_validator(export_model_tool_json_schema(tool)).validate(arguments)
        return dict(arguments)

    # LangChain 的公开 subset model 不继承原模型的 extra 配置，但不得因此
    # 放宽本地工具的 forbid 或丢失 allow；注入字段仍不参与公开模型校验。
    public_model = get_model_tool_schema(tool)
    input_model = cast(type[BaseModel], tool.get_input_schema())
    injected = arguments.keys() & (input_model.model_fields.keys() - public_model.model_fields.keys())
    if injected:
        raise ValueError(f"工具 {tool.name} 的后端注入参数不得由模型提供: {sorted(injected)}")
    parsed = public_model.model_validate(arguments, extra=input_model.model_config.get("extra"))
    # subset 的序列化器仍按其原 ignore 配置编译，显式保留本次 allow 的字段。
    return {**parsed.model_dump(), **(parsed.model_extra or {})}


def filter_tool_event_injected_arguments(
    tool: BaseTool,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """移除工具事件的后端注入值，保留 Provider 原参数及其校验错误。"""
    if not isinstance(arguments, dict):
        raise TypeError(
            "工具事件参数必须是 object: "
            f"tool_name={tool.name} actual_type={type(arguments).__name__}"
        )

    tool_call_schema = getattr(tool, "tool_call_schema", None)
    if not isinstance(tool_call_schema, dict) and not (
        isinstance(tool_call_schema, type)
        and issubclass(tool_call_schema, BaseModel)
    ):
        raise TypeError(
            "工具缺少模型可见的 JSON Schema: "
            f"tool_name={tool.name} tool_type={type(tool).__name__}"
        )
    # 与 LangChain callback 使用同一注入参数过滤规则。观察链不能再次校验、
    # 转换或丢弃原参数，否则 schema 错误调用会失去 canonical claim 身份。
    # 真正的参数校验仍由 BaseTool 执行 owner 完成。
    return tool._filter_injected_args(arguments)


def export_model_tool_json_schema(tool: BaseTool) -> dict[str, Any]:
    """导出发送给模型及请求回放使用的公开 JSON Schema。"""
    tool_call_schema = getattr(tool, "tool_call_schema", None)
    parameters = (
        dict(tool_call_schema)
        if isinstance(tool_call_schema, dict)
        else get_model_tool_schema(tool).model_json_schema()
    )
    if not isinstance(parameters, dict):
        raise TypeError(
            "模型可见工具 JSON Schema 不是 object: "
            f"tool_name={tool.name} tool_type={type(tool).__name__}"
        )
    if not isinstance(tool_call_schema, dict):
        extra = cast(type[BaseModel], tool.get_input_schema()).model_config.get("extra")
        if extra in {"forbid", "allow"}:
            parameters = {**parameters, "additionalProperties": extra == "allow"}
    # 隔离第三方缓存的 schema，所有消费者取得同一 JCS 规范值。
    parameters = json.loads(canonical_json_bytes(parameters))
    model_tool_schema_validator(parameters)
    return parameters
