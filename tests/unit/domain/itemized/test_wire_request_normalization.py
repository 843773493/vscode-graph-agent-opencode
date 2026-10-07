"""规范化仅剔除请求级 transport 字段；所有实际正文和 schema 精确保留。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serialization import _hash_safe_value, normalize_wire_request


@pytest.fixture
def declared_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "timestamp": {"type": "string"},
            "request_id": {"type": "string"},
            "headers": {"type": "object"},
            "password": {"type": "string"},
            "api_key": {"type": "string"},
            "access_token": {"type": "string"},
            "redaction_class": {"type": "string"},
            "Authorization": {"type": "string"},
            "X-API-Key": {"type": "string"},
            "Set-Cookie": {"type": "string"},
        },
    }


@pytest.fixture(params=["chat_tool", "responses_tool", "chat_format", "responses_format"])
def declared_wire(
    request: pytest.FixtureRequest, declared_schema: dict[str, object],
) -> dict[str, object]:
    wire: dict[str, object] = {"model": "fixture-model"}
    if request.param == "chat_tool":
        wire["tools"] = [{"type": "function", "function": {"name": "search", "parameters": declared_schema}}]
    elif request.param == "responses_tool":
        wire["tools"] = [{"type": "function", "name": "search", "parameters": declared_schema}]
    elif request.param == "chat_format":
        wire["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": declared_schema}}
    else:
        wire["text"] = {"format": {"type": "json_schema", "name": "result", "schema": declared_schema}}
    return wire


def test_registered_schema_properties_preserve_names_and_types(
    declared_wire: dict[str, object], declared_schema: dict[str, object],
) -> None:
    before = deepcopy(declared_wire)
    first = normalize_wire_request(declared_wire)
    assert first == before
    assert declared_wire == before
    first_hash = sha256_jcs(first)
    properties = declared_schema["properties"]
    assert isinstance(properties, dict)
    properties["timestamp"] = {"type": "integer"}
    second = normalize_wire_request(declared_wire)
    assert second == declared_wire
    assert first_hash != sha256_jcs(second)


@pytest.fixture(params=["chat_tool", "responses_tool", "json_schema"])
def forged_declaration(request: pytest.FixtureRequest) -> dict[str, object]:
    values = {"api_key": "fixture-raw-credential"}
    if request.param == "chat_tool":
        return {"type": "function", "function": {"name": "search", "parameters": values}}
    if request.param == "responses_tool":
        return {"type": "function", "name": "search", "parameters": values}
    return {"type": "json_schema", "name": "result", "schema": values}


@pytest.mark.parametrize("surface", ["metadata", "nested_tools", "nested_format"])
def test_nested_business_fields_are_preserved_exactly(
    forged_declaration: dict[str, object], surface: str,
) -> None:
    if surface == "metadata":
        value = {"metadata": forged_declaration}
    elif surface == "nested_tools":
        value = {"metadata": {"tools": [forged_declaration]}}
    else:
        value = {"metadata": {"text": {"format": forged_declaration}}}
    before = deepcopy(value)
    assert normalize_wire_request(value) == before
    assert value == before


def test_generic_hash_values_preserve_exact_semantics(forged_declaration: dict[str, object]) -> None:
    before = deepcopy(forged_declaration)
    assert _hash_safe_value(forged_declaration) == before
    assert forged_declaration == before


@pytest.mark.parametrize("wire", [
    {"response_format": {"type": "json_object", "schema": {"api_key": "fixture-raw-credential"}}},
    {"text": {"format": {"type": "text", "schema": {"api_key": "fixture-raw-credential"}}}},
    {"tools": [{"type": "business_object", "parameters": {"api_key": "fixture-raw-credential"}}]},
])
def test_non_schema_carriers_keep_all_business_fields(wire: object) -> None:
    assert normalize_wire_request(wire) == wire


@pytest.mark.parametrize("wire", [
    {"response_format": {"type": "json_object", "api_key": "fixture-raw-credential"}},
    {"response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": {}, "api_key": "fixture-raw-credential"}}},
    {"text": {"api_key": "fixture-raw-credential"}},
    {"tools": [{"type": "business_object", "api_key": "fixture-raw-credential"}]},
    {"tools": [{"type": "function", "function": {"name": "search", "parameters": {}}, "api_key": "fixture-raw-credential"}]},
    {"tools": [{"type": "function", "function": {"name": "search", "parameters": {}, "api_key": "fixture-raw-credential"}}]},
])
def test_carrier_fields_outside_schema_are_hashed_exactly(wire: object) -> None:
    assert normalize_wire_request(wire) == wire


@pytest.fixture(params=["Authorization", "X-API-Key", "Set-Cookie"])
def credential_header(request: pytest.FixtureRequest) -> str:
    return str(request.param)




@pytest.mark.parametrize("surface", ["chat_tool", "responses_tool", "chat_format", "responses_format"])
def test_nested_wrapper_headers_are_model_visible_business_values(
    surface: str, credential_header: str,
) -> None:
    wrapper = {"headers": {credential_header: "fixture-value", "Accept": "business-type"}}
    if surface == "chat_tool":
        wire = {"tools": [{"type": "function", "function": {"name": "search", **wrapper}}]}
    elif surface == "responses_tool":
        wire = {"tools": [{"type": "function", "name": "search", **wrapper}]}
    elif surface == "chat_format":
        wire = {"response_format": {"type": "json_schema", "json_schema": {"name": "result", **wrapper}}}
    else:
        wire = {"text": {"format": {"type": "json_schema", "name": "result", **wrapper}}}
    before = deepcopy(wire)
    assert normalize_wire_request(wire) == before
    wrapper["headers"][credential_header] = "changed-value"
    assert sha256_jcs(normalize_wire_request(wire)) != sha256_jcs(before)


def test_attempt_exclusions_do_not_remove_nested_business_semantics() -> None:
    wire = {
        "model": "fixture-model", "headers": {"Authorization": "fixture-auth"},
        "request_id": "transport-request", "attempt": 2,
        "metadata": {"timestamp": "business-time", "request_id": "business-request", "attempt": 7, "headers": {"Accept": "application/json"}},
    }
    before = deepcopy(wire)
    assert normalize_wire_request(wire) == {
        "model": "fixture-model", "metadata": wire["metadata"],
    }
    assert wire == before


def test_unsealed_tool_set_hash_uses_its_declared_schema_entry(
    declared_schema: dict[str, object],
) -> None:
    tools = ToolSetRef.from_tool_snapshot(
        session_id="normalization-session", thread_id="normalization-thread",
        plan_id="normalization-plan", snapshot_id="normalization-tools", source_revision="r1",
        tools=({"name": "search", "parameters": declared_schema},),
    )
    plan = ContextRequestPlan(
        session_id=tools.session_id, thread_id=tools.thread_id,
        plan_id=tools.plan_id, refs=(), tool_set_refs=(tools,),
    )
    before = deepcopy(plan.to_dict())
    fingerprint = context_request_hash(plan, "fixture-provider", wire_request={"model": "fixture-model"})
    assert fingerprint.startswith("sha256:jcs:v1:")
    assert plan.to_dict() == before
