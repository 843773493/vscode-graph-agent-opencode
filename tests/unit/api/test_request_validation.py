"""经真实路由和统一响应边界核对请求校验，不回显原生输入或动态对象键。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated
from unittest.mock import MagicMock

import pytest
from fastapi import Body, FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator

from app.api.deps import (
    get_workspace_service,
    verify_local_token,
    workspace_container_scope,
)
from app.core.path_utils import workspace_context
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.main import app as gateway_app
from app.gateway.routes._shared import get_registry
from app.main import app as workspace_app

_REQUEST_ID = "request-validation-contract"


@pytest.fixture(params=["workspace", "gateway"])
def validation_client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, empty_workspace_scope: tuple[str, Path]) -> Iterator[tuple[TestClient, MagicMock, str]]:
    owner = MagicMock()
    application = workspace_app if request.param == "workspace" else gateway_app
    workspace_id, workspace_root = empty_workspace_scope

    async def bind_workspace() -> AsyncIterator[None]:
        with workspace_context(workspace_id, workspace_root):
            yield

    for dependency, override in ((get_workspace_service, lambda: owner), (get_registry, lambda: owner),
                                 (verify_local_token, lambda: "test-token"), (verify_gateway_token, lambda: "test-token"),
                                 (workspace_container_scope, bind_workspace)):
        monkeypatch.setitem(application.dependency_overrides, dependency, override)
    client = TestClient(application, raise_server_exceptions=False, headers={
        "X-BoxTeam-Workspace-Id": workspace_id, "X-Request-ID": _REQUEST_ID,
    })
    try:
        yield client, owner, request.param
    finally:
        client.close()


@pytest.mark.parametrize("native", ["/private/validation-sentinel.txt", r"C:\private\validation-sentinel.txt", r"\\server\share\validation-sentinel.txt"])
def test_actual_routes_reject_without_echo_or_service_execution(validation_client, native: str, caplog: pytest.LogCaptureFixture) -> None:
    client, owner, surface = validation_client
    if surface == "workspace":
        response = client.put("/api/v1/workspace/files/content", params={"path": native},
                              json={"content": native, "expected_revision": native})
        expected_field, expected_type = "expected_revision", "string_too_short"
    else:
        response = client.post("/api/gateway/workspaces/local", json={"root_path": [native]})
        expected_field, expected_type = "root_path", "string_type"
    assert response.status_code == 422, response.text
    assert "validation-sentinel" not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == _REQUEST_ID
    assert response.json()["detail"] == [{"loc": ["body", expected_field], "type": expected_type, "msg": "请求参数校验失败"}]
    assert owner.mock_calls == []
    assert "validation-sentinel" in caplog.text  # 原始异常仍在服务端，可定位实际输入。


class NamedInput(BaseModel):
    count: int = Field(alias="total")


class StructuredInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[dict[str, int]]
    text: str = "normal"
    named: dict[str, NamedInput] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def reject_text(cls, value: str) -> str:
        if value != "normal":
            raise ValueError(f"拒绝输入: {value}")
        return value


@pytest.fixture(params=[workspace_app, gateway_app], ids=["workspace", "gateway"])
def structured_client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.add_exception_handler(RequestValidationError, request.param.exception_handlers[RequestValidationError])

    @application.post("/validation")
    def validate(payload: StructuredInput):
        return payload

    @application.post("/embedded")
    def embedded(payload: Annotated[StructuredInput, Body(embed=True)]):
        return payload

    @application.get("/query")
    def query(payload: Annotated[NamedInput, Query()]):
        return payload

    @application.post("/union")
    def union(payload: NamedInput | list[NamedInput]):
        return payload

    @application.post("/mapping-union")
    def mapping_union(payload: NamedInput | dict[str, int]):
        return payload

    @application.post("/composition")
    def composition(payload: Annotated[NamedInput, WithJsonSchema({"allOf": [
        {"type": "object", "properties": {"total": {"type": "integer"}}},
        {"type": "object", "properties": {"description": {"type": "string"}}},
    ]})]):
        return payload

    with TestClient(application, raise_server_exceptions=False, headers={"X-Request-ID": _REQUEST_ID}) as client:
        yield client


@pytest.mark.parametrize("native", ["/private/validation-sentinel.txt", r"C:\private\validation-sentinel.txt", r"\\server\share\validation-sentinel.txt"])
def test_dynamic_keys_values_and_custom_errors_use_structural_coordinates(structured_client: TestClient, native: str) -> None:
    response = structured_client.post("/validation", json={"entries": [{native: native}], "text": native, native: native})
    assert response.status_code == 422, response.text
    assert "validation-sentinel" not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == _REQUEST_ID
    assert response.json()["detail"] == [
        {"loc": ["body", "entries", 0, "member[0]"], "type": "int_parsing", "msg": "请求参数校验失败"},
        {"loc": ["body", "text"], "type": "value_error", "msg": "请求参数校验失败"},
        {"loc": ["body", "member[2]"], "type": "extra_forbidden", "msg": "请求参数校验失败"},
    ]


def test_missing_fields_and_malformed_json_keep_precise_failure_categories(structured_client: TestClient) -> None:
    missing = structured_client.post("/validation", json={})
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"] == [{"loc": ["body", "entries"], "type": "missing", "msg": "请求参数校验失败"}]
    malformed = structured_client.post("/validation", content='{"entries": /private/validation-sentinel}', headers={"Content-Type": "application/json"})
    assert malformed.status_code == 422, malformed.text
    assert "validation-sentinel" not in malformed.text
    assert malformed.json()["detail"][0]["loc"] == ["body", 12]
    assert malformed.json()["detail"][0]["type"] == "json_invalid"


@pytest.mark.parametrize("key", ["path", "body", "text", "entries", "[key]"])
def test_mapping_keys_cannot_borrow_declared_names(structured_client: TestClient, key: str) -> None:
    payload = {"entries": [{"valid": 1, key: "invalid"}], "named": {key: {"total": "invalid"}}}
    for endpoint, body, prefix in (("/validation", payload, ["body"]), ("/embedded", {"payload": payload}, ["body", "payload"])):
        response = structured_client.post(endpoint, json=body)
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == [
            {"loc": [*prefix, "entries", 0, "member[1]"], "type": "int_parsing", "msg": "请求参数校验失败"},
            {"loc": [*prefix, "named", "member[0]", "total"], "type": "int_parsing", "msg": "请求参数校验失败"},
        ]


def test_aliases_query_models_and_union_references_keep_declared_fields(structured_client: TestClient) -> None:
    query = structured_client.get("/query", params={"total": "invalid"})
    assert query.status_code == 422, query.text
    assert query.json()["detail"] == [{"loc": ["query", "total"], "type": "int_parsing", "msg": "请求参数校验失败"}]
    union = structured_client.post("/union", json=[{"total": "invalid"}])
    assert union.status_code == 422, union.text
    assert union.json()["detail"] == [
        {"loc": ["body", "<schema>"], "type": "model_attributes_type", "msg": "请求参数校验失败"},
        {"loc": ["body", "<schema>", 0, "member[0]"], "type": "int_parsing", "msg": "请求参数校验失败"},
    ]


def test_union_mapping_cannot_borrow_sibling_model_fields(structured_client: TestClient) -> None:
    response = structured_client.post("/mapping-union", json={"total": "invalid"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {"loc": ["body", "<schema>", "member[0]"], "type": "int_parsing", "msg": "请求参数校验失败"},
        {"loc": ["body", "<schema>", "member[0]"], "type": "int_parsing", "msg": "请求参数校验失败"},
    ]


@pytest.mark.parametrize(("payload", "rule"), [({"total": "invalid"}, "int_parsing"), ({}, "missing")])
def test_composed_schema_keeps_declared_field_coordinates(structured_client: TestClient, payload: dict[str, str], rule: str) -> None:
    response = structured_client.post("/composition", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [{"loc": ["body", "total"], "type": rule, "msg": "请求参数校验失败"}]
