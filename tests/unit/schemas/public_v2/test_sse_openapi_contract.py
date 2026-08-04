from __future__ import annotations

import json
from pathlib import Path

from app.main import app
from app.schemas.public_v2.sse import SSE_EVENT_MODELS


def test_sse_endpoints_publish_event_contracts() -> None:
    paths = app.openapi()["paths"]

    workspace_response = paths["/api/v1/workspace/files/events"]["post"]["responses"][
        "200"
    ]
    assert workspace_response["x-sse-events"] == {
        "changes": {"$ref": "#/components/schemas/WorkspaceFileChangeBatchDTO"},
        "error": {"$ref": "#/components/schemas/SseErrorDTO"},
    }
    assert "text/event-stream" in workspace_response["content"]
    assert "application/json" not in workspace_response["content"]

    trace_response = paths["/api/v1/sessions/{session_id}/traces/stream"]["get"][
        "responses"
    ]["200"]
    assert trace_response["x-sse-events"] == {
        "trace": {"$ref": "#/components/schemas/TraceEventDTO"}
    }
    assert set(trace_response["content"]) == {"text/event-stream"}

    job_response = paths["/api/v1/jobs/{job_id}/events/stream"]["get"]["responses"][
        "200"
    ]
    assert job_response["x-sse-events"] == {
        "*": {"$ref": "#/components/schemas/SessionExecutionSseDTO"}
    }
    assert set(job_response["content"]) == {"text/event-stream"}


def test_every_sse_event_model_is_resolvable_and_generated() -> None:
    openapi = app.openapi()
    components = openapi["components"]["schemas"]
    declared_models: set[str] = set()
    stream_operation_count = 0
    for path_item in openapi["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            response = operation.get("responses", {}).get("200", {})
            if "text/event-stream" not in response.get("content", {}):
                continue
            stream_operation_count += 1
            events = response.get("x-sse-events")
            assert isinstance(events, dict) and events
            for event_schema in events.values():
                reference = event_schema["$ref"]
                prefix = "#/components/schemas/"
                assert reference.startswith(prefix)
                model_name = reference.removeprefix(prefix)
                assert model_name in components
                declared_models.add(model_name)

    assert stream_operation_count == 3
    assert declared_models == set(SSE_EVENT_MODELS)

    runtime_schema_path = (
        Path.cwd()
        / "src"
        / "web"
        / "src"
        / "types"
        / "gen"
        / "sse_runtime_schemas.json"
    )
    runtime_schemas = json.loads(runtime_schema_path.read_text(encoding="utf-8"))[
        "schemas"
    ]
    assert set(runtime_schemas) == set(SSE_EVENT_MODELS)

    generated_types = runtime_schema_path.with_name("session_interaction.ts").read_text(
        encoding="utf-8"
    )
    assert (
        'export type SessionExecutionEventDTO = SessionExecutionSseDTO["event"];'
        in generated_types
    )
