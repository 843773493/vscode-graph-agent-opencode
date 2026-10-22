"""Gateway 与工作区共用的请求校验错误投影，不把输入当作诊断输出。"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import TypedDict

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import JsonSchemaValue

from app.core.trace_middleware import get_request_id

logger = logging.getLogger(__name__)


class ValidationIssue(TypedDict):
    loc: list[str | int]
    type: str
    msg: str


SchemaNode = tuple[JsonSchemaValue, JsonSchemaValue]


def _request_schemas(request: Request) -> dict[str, list[SchemaNode]]:
    """保留参数所属位置及 schema 层级，不能把同名动态键当成声明字段。"""
    schemas: dict[str, list[SchemaNode]] = {name: [] for name in ("body", "query", "path", "header", "cookie")}
    body = request.scope["route"].body_field
    if body is not None:
        schema = TypeAdapter(body.field_info.rebuild_annotation()).json_schema()
        schemas["body"].append((schema, schema))
    dependencies = [request.scope["route"].dependant]
    while dependencies:
        dependency = dependencies.pop()
        dependencies.extend(dependency.dependencies)
        for category in ("query", "path", "header", "cookie"):
            fields = getattr(dependency, category + "_params")
            for field in fields:
                annotation = field.field_info.annotation
                schema = TypeAdapter(field.field_info.rebuild_annotation()).json_schema()
                if len(fields) == 1 and isinstance(annotation, type) and issubclass(annotation, BaseModel):
                    node = schema  # FastAPI 的单模型参数直接展开到所属位置。
                else:
                    validation_alias = field.field_info.validation_alias
                    alias = validation_alias if isinstance(validation_alias, str) else field.alias
                    node = {"properties": {alias: schema}}
                schemas[category].append((node, schema))
    return schemas


def _variants(nodes: list[SchemaNode]) -> Iterator[SchemaNode]:
    """展开引用和单一非空分支，不把多个 union 分支的字段混在一起。"""
    for schema, root in nodes:
        if "$ref" in schema:
            referenced = root
            for part in schema["$ref"].removeprefix("#/").split("/"):
                referenced = referenced[part.replace("~1", "/").replace("~0", "~")]
            yield from _variants([(referenced, root)])
        else:
            branches = schema.get("anyOf", schema.get("oneOf"))
            concrete = [branch for branch in branches if branch.get("type") != "null"] if branches else []
            if len(concrete) == 1:
                yield from _variants([(concrete[0], root)])
            else:
                yield schema, root
                yield from _variants([(branch, root) for branch in schema.get("allOf", [])])


def _public_location(location: tuple[str | int, ...], inputs: Mapping[str, object], schemas: dict[str, list[SchemaNode]]) -> list[str | int]:
    """动态对象键使用零基成员坐标；数组下标与声明字段保留原语义。"""
    category, *parts = location
    result: list[str | int] = [category if category in schemas else "<schema>"]
    document: object = inputs.get(category)
    nodes = schemas.get(category, [])
    for part in parts:
        variants = list(_variants(nodes))
        if isinstance(part, str) and any("anyOf" in schema or "oneOf" in schema for schema, _ in variants):
            # Pydantic 分支标签不等于 JSON Schema 标题；无法可靠对应时只公开结构坐标。
            result.append("<schema>")
            nodes = [({}, root) for _, root in variants]
            continue
        declared = [(schema["properties"][part], root) for schema, root in variants if part in schema.get("properties", {})]
        if declared:
            result.append(part)
            nodes = declared
            document = document.get(part) if isinstance(document, dict) else None
        elif isinstance(document, dict) and part in document:
            result.append(f"member[{list(document).index(part)}]")
            nodes = [(schema["additionalProperties"], root) for schema, root in variants
                     if isinstance(schema.get("additionalProperties"), dict)]
            document = document[part]
        elif isinstance(part, int):
            result.append(part)
            nodes = [(schema["prefixItems"][part] if part < len(schema.get("prefixItems", [])) else schema.get("items", {}), root)
                     for schema, root in variants]
            document = document[part] if isinstance(document, list) and 0 <= part < len(document) else None
        else:
            # union 分支等标签不复制输入字符串，下一段仍可定位到分支字段。
            result.append("<schema>")
    return result


async def request_validation_error_response(request: Request, error: RequestValidationError) -> JSONResponse:
    """保持 422 与错误规则，原始输入和自定义异常只留在服务端诊断中。"""
    request_id = get_request_id(request)
    failures = error.errors()
    logger.warning("请求校验失败 request_id=%s", request_id, exc_info=error)
    schemas = _request_schemas(request)
    inputs = {"body": error.body, "query": dict(request.query_params), "path": request.path_params,
              "header": dict(request.headers), "cookie": request.cookies}
    issues: list[ValidationIssue] = [
        {"loc": _public_location(failure["loc"], inputs, schemas),
         "type": failure["type"], "msg": "请求参数校验失败"}
        for failure in failures
    ]
    return JSONResponse(status_code=422, headers={"X-Request-ID": request_id},
                        content={"detail": issues, "request_id": request_id})
