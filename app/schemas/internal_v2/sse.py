from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field
from pydantic.json_schema import models_json_schema

from app.protocol.openapi import protobuf_message_name_for_model
from app.schemas.internal_v2.session_interaction import SessionExecutionSseDTO
from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.workspace import WorkspaceFileChangeBatchDTO


class SseErrorDTO(BaseModel):
    message: str = Field(min_length=1)


SSE_EVENT_MODELS: dict[str, type[BaseModel]] = {
    model.__name__: model
    for model in (
        TraceEventDTO,
        SessionExecutionSseDTO,
        WorkspaceFileChangeBatchDTO,
        SseErrorDTO,
    )
}


def sse_responses(
    description: str,
    events: Mapping[str, type[BaseModel]],
) -> dict[int, dict[str, Any]]:
    event_refs: dict[str, dict[str, str]] = {}
    for event_name, model in events.items():
        registered = SSE_EVENT_MODELS.get(model.__name__)
        if registered is not model:
            raise RuntimeError(f"SSE DTO 未注册: {model.__name__}")
        event_refs[event_name] = {
            "$ref": f"#/components/schemas/{model.__name__}",
        }
        protobuf_name = protobuf_message_name_for_model(model.__name__)
        if protobuf_name is not None:
            event_refs[event_name]["x-protobuf-message"] = protobuf_name
    return {
        200: {
            "description": description,
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
            "x-sse-events": event_refs,
        }
    }


def install_sse_openapi_components(app: FastAPI) -> None:
    original_openapi = app.openapi

    def openapi_with_sse_components() -> dict[str, Any]:
        schema = original_openapi()
        _, root_schema = models_json_schema(
            [(model, "serialization") for model in SSE_EVENT_MODELS.values()],
            ref_template="#/components/schemas/{model}",
        )
        generated = root_schema.get("$defs", {})
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, model_schema in generated.items():
            # FastAPI 已登记的同名模型使用 validation schema，SSE 补充项使用
            # serialization schema；同一 Pydantic 模型的 optional default 表示会不同。
            protobuf_name = protobuf_message_name_for_model(name)
            if protobuf_name is not None:
                model_schema["x-protobuf-message"] = protobuf_name
            components.setdefault(name, model_schema)
        return schema

    app.openapi = openapi_with_sse_components
