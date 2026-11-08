"""规范资源 grammar、通信 read owner 与现有 inventory/search 的工具合同。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from app.abstractions.session_context import (
    SessionContextRevisionChangedError,
    SessionContextSearchPatternError,
)
from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.agents.tools.session_history import (
    create_read_context_tool,
    create_search_context_tool,
)
from app.core import failures
from app.core.session_control_communication_ledger.observations import (
    RemoteObservationError,
)
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadResultDTO,
    SessionContextSearchResultDTO,
)
from app.schemas.search_federation import GatewayContextSearchResultDTO
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.support.communication.probe import (
    SOURCE_SESSION,
    TARGET_SESSION,
    WORKSPACE_ID,
)

REMOTE_WORKSPACE = "33333333-3333-4333-8333-333333333333"


def _session_input(workspace_id: str = WORKSPACE_ID) -> dict[str, str]:
    return {"resource_identity": str(session_resource_identity(workspace_id, TARGET_SESSION)),
            "uri": session_resource_uri(workspace_id, TARGET_SESSION), "scope": "workspace"}


class QueryProbe:
    workspace_id = WORKSPACE_ID

    def __init__(self):
        self.calls = []

    async def read_context(self, request):
        self.calls.append(("read", request))
        return SessionContextReadResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "view"}), revision="local-read")

    async def search_context(self, request):
        self.calls.append(("search", request))
        return SessionContextSearchResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "query", "match_mode"}), revision="local-search")


class WorkspaceProbe:
    def __init__(self):
        self.calls = []

    async def read_gateway_context(self, request):
        self.calls.append(("gateway-read", request))
        return SessionContextReadResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "view"}), revision="gateway-read")

    async def read_resource_context(self, request):
        self.calls.append(("resource-read", request))
        return SessionContextReadResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "view"}), revision="remote-read")

    async def search_gateway_context(self, request):
        self.calls.append(("gateway-search", request))
        return GatewayContextSearchResultDTO(events=[])

    async def search_resource_context(self, request):
        self.calls.append(("resource-search", request))
        return SessionContextSearchResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "query", "match_mode"}), revision="remote-search")


@pytest.fixture
def history_context(communication_probe, communication_invocation):
    return SimpleNamespace(
        session_id=SOURCE_SESSION, session_context_query_service=QueryProbe(),
        workspace_session_context_client=WorkspaceProbe(),
        session_communication_service=communication_probe, invocation_context=communication_invocation,
    )


async def test_session_read_uses_durable_communication_owner(history_context, communication_probe):
    arguments = _session_input()
    communication_probe.read_result = SessionContextReadResultDTO.model_validate({**arguments, "view": "overview", "revision": "frozen"})
    result = json.loads(await create_read_context_tool(history_context).ainvoke(arguments))
    assert result["revision"] == "frozen"
    operation, call = communication_probe.calls[0]
    assert operation == "read" and call["session_id"] == SOURCE_SESSION
    assert call["thread_id"] == history_context.invocation_context.thread_binding.thread_id
    assert call["source_call_id"] == history_context.invocation_context.require_operation_id("read")
    assert history_context.session_context_query_service.calls == []


async def test_search_preserves_resource_identity_without_gateway_route_mapping(history_context):
    arguments = {**_session_input(REMOTE_WORKSPACE), "query": "ALPHA"}
    result = json.loads(await create_search_context_tool(history_context).ainvoke(arguments))
    assert result["revision"] == "remote-search" and result["uri"] == arguments["uri"]
    route, request = history_context.workspace_session_context_client.calls[0]
    assert route == "resource-search" and request.uri == arguments["uri"]
    assert request.resource_identity == arguments["resource_identity"]


async def test_local_search_keeps_current_workspace_query(history_context):
    result = json.loads(await create_search_context_tool(history_context).ainvoke({**_session_input(), "query": "ALPHA"}))
    assert result["revision"] == "local-search"
    assert history_context.session_context_query_service.calls[0][0] == "search"


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("enveloped", [False, True])
async def test_invalid_regex_tool_keeps_typed_error_and_original_request_identity(
    history_context, monkeypatch: pytest.MonkeyPatch, remote: bool, enveloped: bool,
) -> None:
    error = SessionContextSearchPatternError("query 不是有效正则表达式: unterminated character set at position 0")
    query = AsyncMock(side_effect=error)
    service = history_context.workspace_session_context_client if remote else history_context.session_context_query_service
    monkeypatch.setattr(service, "search_resource_context" if remote else "search_context", query)
    target = create_search_context_tool(history_context)
    tool = (create_extension_tool_invoker_tool([target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]))
            if enveloped else target)
    arguments = {**_session_input(REMOTE_WORKSPACE if remote else WORKSPACE_ID), "query": "[", "match_mode": "regex"}
    token = set_current_request_id("req_invalid_context_regex")
    try:
        result = await tool.ainvoke({"type": "tool_call", "id": "invalid_regex", "name": tool.name,
                                    "args": {"tool_name": target.name, "arguments": arguments} if enveloped else arguments})
    finally:
        reset_current_request_id(token)
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert json.loads(result.text) == {**error.detail(), "request_id": "req_invalid_context_regex"}
    query.assert_awaited_once()


async def test_gateway_inventory_requires_current_gateway_identity(history_context):
    token = set_current_gateway_id("gateway_4d6aaa9a55e0dcf999456634e90e0f67")
    try:
        arguments = {"resource_identity": str(gateway_session_inventory_identity("gateway_4d6aaa9a55e0dcf999456634e90e0f67")),
                     "uri": gateway_session_inventory_uri("gateway_4d6aaa9a55e0dcf999456634e90e0f67"), "scope": "gateway", "view": "inventory"}
        result = json.loads(await create_read_context_tool(history_context).ainvoke(arguments))
        assert result["revision"] == "gateway-read"
        assert history_context.workspace_session_context_client.calls[0][0] == "gateway-read"
    finally:
        reset_current_gateway_id(token)


async def test_workspace_inventory_keeps_cold_query(history_context):
    arguments = {"resource_identity": str(workspace_session_inventory_identity(WORKSPACE_ID)),
                 "uri": workspace_session_inventory_uri(WORKSPACE_ID), "scope": "workspace", "view": "inventory"}
    result = json.loads(await create_read_context_tool(history_context).ainvoke(arguments))
    assert result["revision"] == "local-read"
    assert history_context.session_context_query_service.calls[0][0] == "read"


async def test_read_rejects_non_boxteam_resource_as_tool_error(history_context):
    tool = create_read_context_tool(history_context)
    result = await tool.ainvoke({"type": "tool_call", "id": "call_bad_resource", "name": tool.name,
                                "args": {**_session_input(), "uri": "file:///context.jsonl"}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "boxteam://" in result.text


@pytest.mark.parametrize("identity", [
    "/private/context/resource-id", r"C:\private\context\resource-id", r"\\target\private\context\resource-id",
])
@pytest.mark.parametrize("operation", ["read", "search"])
@pytest.mark.parametrize("enveloped", [False, True])
async def test_context_identity_rejection_keeps_path_out_of_model_result_and_allows_correction(
    history_context, communication_probe, caplog: pytest.LogCaptureFixture,
    identity: str, operation: str, enveloped: bool,
):
    valid = _session_input()
    communication_probe.read_result = SessionContextReadResultDTO.model_validate({**valid, "view": "overview", "revision": "frozen"})
    if operation == "search":
        valid["query"] = "needle"
    target = (create_read_context_tool if operation == "read" else create_search_context_tool)(history_context)
    tool = create_extension_tool_invoker_tool(
        [target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]),
    ) if enveloped else target
    invalid = {**valid, "resource_identity": identity}
    call = {"type": "tool_call", "id": "invalid-resource-identity", "name": tool.name,
            "args": {"tool_name": target.name, "arguments": invalid} if enveloped else invalid}
    result = await tool.ainvoke(call)
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert (result.name, result.tool_call_id) == (tool.name, call["id"])
    assert result.text == "ResourceIdentity resource_identity 不得携带路径/URI/credential 形态"
    assert identity not in result.text and "private" not in result.model_dump_json()
    assert communication_probe.calls == history_context.session_context_query_service.calls == []
    assert history_context.workspace_session_context_client.calls == []
    assert invalid == {**valid, "resource_identity": identity}
    errors = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    assert errors[0].__notes__ == [f"被拒绝的 ResourceIdentity resource_identity: {identity!r}"]

    call["id"] = "corrected-resource-identity"
    call["args"] = {"tool_name": target.name, "arguments": valid} if enveloped else valid
    corrected = await tool.ainvoke(call)
    assert isinstance(corrected, ToolMessage) and corrected.status == "success"
    assert (corrected.name, corrected.tool_call_id) == (tool.name, call["id"])
    assert json.loads(corrected.text)["resource_identity"] == valid["resource_identity"]
    assert len(communication_probe.calls if operation == "read" else history_context.session_context_query_service.calls) == 1


async def test_identity_diagnostic_write_failure_preserves_rejection_and_next_call(
    history_context, communication_probe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    blocker = tmp_path / "diagnostic-private"
    blocker.write_text("普通文件不是日志目录。", encoding="utf-8")
    captured: list[BaseException] = []

    def blocked_sink(
        *_args: object, exc_info: BaseException, extra: Mapping[str, object] | None = None,
    ) -> None:
        assert extra is None
        captured.append(exc_info)
        (blocker / "service.log").write_text("原诊断", encoding="utf-8")

    monkeypatch.setattr(failures.logger, "error", blocked_sink)
    valid = _session_input()
    communication_probe.read_result = SessionContextReadResultDTO.model_validate({**valid, "view": "overview", "revision": "frozen"})
    tool = create_read_context_tool(history_context)
    result = await tool.ainvoke({"type": "tool_call", "id": "diagnostic-failed", "name": tool.name,
                                "args": {**valid, "resource_identity": "/private/context/resource-id"}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert result.text == (
        "ResourceIdentity resource_identity 不得携带路径/URI/credential 形态, diagnostic_sink_error=NotADirectoryError"
    )
    assert "private" not in result.model_dump_json()
    assert len(captured) == 1 and isinstance(captured[0], ValueError)
    assert captured[0].args == ("ResourceIdentity resource_identity 不得携带路径/URI/credential 形态",)
    assert captured[0].__notes__[0] == "被拒绝的 ResourceIdentity resource_identity: '/private/context/resource-id'"
    assert str(blocker) in captured[0].__notes__[1] and "NotADirectoryError" in captured[0].__notes__[1]
    assert communication_probe.calls == []
    corrected = await tool.ainvoke({"type": "tool_call", "id": "after-diagnostic-failed", "name": tool.name, "args": valid})
    assert isinstance(corrected, ToolMessage) and corrected.status == "success"
    assert corrected.tool_call_id == "after-diagnostic-failed" and len(communication_probe.calls) == 1


async def test_custom_invoker_propagates_revision_conflict(history_context, communication_probe):
    communication_probe.error = SessionContextRevisionChangedError(expected_revision="old", actual_revision="new")
    target = create_read_context_tool(history_context)
    invoker = create_extension_tool_invoker_tool([target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]))
    result = await invoker.ainvoke({"type": "tool_call", "id": "call_changed", "name": invoker.name,
                                  "args": {"tool_name": target.name, "arguments": {**_session_input(), "revision": "old"}}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "expected=old" in result.text and "actual=new" in result.text


async def test_custom_invoker_exposes_observation_snapshot_error(history_context, communication_probe):
    communication_probe.error = RemoteObservationError("read-snapshot-mismatch")
    target = create_read_context_tool(history_context)
    invoker = create_extension_tool_invoker_tool([target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]))
    result = await invoker.ainvoke({"type": "tool_call", "id": "call_stale_snapshot", "name": invoker.name,
                                  "args": {"tool_name": target.name, "arguments": _session_input()}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert result.text == "read-snapshot-mismatch"


@pytest.mark.parametrize("operation", ["read", "search"])
@pytest.mark.parametrize("enveloped", [False, True])
@pytest.mark.parametrize("unknown_key", [
    "unexpected", "/private/context/argument.json", r"C:\private\context\argument.json",
    r"\\server\context\argument.json",
])
async def test_context_tools_reject_unknown_arguments_before_owner_and_allow_correction(
    history_context, communication_probe, operation: str, enveloped: bool, unknown_key: str,
):
    identity = _session_input()
    communication_probe.read_result = SessionContextReadResultDTO.model_validate({**identity, "view": "overview", "revision": "frozen"})
    target = (create_read_context_tool if operation == "read" else create_search_context_tool)(history_context)
    tool = create_extension_tool_invoker_tool(
        [target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]),
    ) if enveloped else target
    # 合法参数继续沿用本地模型的数值转换与默认值，额外字段不应借此被丢弃。
    valid = {**identity, **({"limit": "7"} if operation == "read" else {"query": "needle", "max_results": "7"})}
    arguments = {**valid, unknown_key: "不得被静默忽略"}
    call = {"type": "tool_call", "id": "invalid-context-arguments", "name": tool.name,
            "args": {"tool_name": target.name, "arguments": arguments} if enveloped else arguments}
    if enveloped:
        rejected = await tool.ainvoke(call)
        assert isinstance(rejected, ToolMessage) and rejected.status == "error"
        assert (rejected.name, rejected.tool_call_id) == (tool.name, call["id"])
        assert "参数校验失败" in rejected.text and "extra_forbidden" in rejected.text
        issues = json.loads(rejected.text.split("issues=", 1)[1])
        assert issues == [{"parameter": unknown_key if unknown_key.isidentifier() else None,
                           "depth": 1, "rule": "extra_forbidden"}]
        assert "private" not in rejected.text and "不得被静默忽略" not in rejected.text
    else:
        with pytest.raises(ValidationError) as rejected_error:
            await tool.ainvoke(call)
        issue, = rejected_error.value.errors()
        assert (issue["loc"], issue["type"], issue["input"]) == ((unknown_key,), "extra_forbidden", arguments[unknown_key])
    assert communication_probe.calls == history_context.session_context_query_service.calls == []
    assert history_context.workspace_session_context_client.calls == []
    assert arguments == {**valid, unknown_key: "不得被静默忽略"}

    call["id"] = "corrected-context-arguments"
    call["args"] = {"tool_name": target.name, "arguments": valid} if enveloped else valid
    corrected = await tool.ainvoke(call)
    assert isinstance(corrected, ToolMessage) and corrected.status == "success"
    assert (corrected.name, corrected.tool_call_id) == (tool.name, call["id"])
    if operation == "read":
        _, received = communication_probe.calls[0]
        request = received["request"]
        assert len(communication_probe.calls) == 1 and request.limit == 7
        assert request.recent_rounds == 3 and request.include == ["visible_text", "tool_summary"]
    else:
        _, request = history_context.session_context_query_service.calls[0]
        assert len(history_context.session_context_query_service.calls) == 1
        assert request.max_results == 7 and request.sources == ["effective_context"]


async def test_session_read_without_owner_fails_loudly(history_context):
    history_context.session_communication_service = None
    with pytest.raises(RuntimeError, match="持久通信 owner"):
        await create_read_context_tool(history_context).ainvoke(_session_input())


def test_read_schema_contains_only_business_parameters(history_context):
    schema = export_model_tool_json_schema(create_read_context_tool(history_context))["properties"]
    assert {"resource_identity", "uri", "scope", "cursor", "revision"} <= schema.keys()
    assert not {"resource", "workspace_id", "source_call_id", "thread_id", "observation_id"} & schema.keys()


@pytest.mark.parametrize("authority", [None, "gateway_remote"])
@pytest.mark.parametrize("operation", ["read", "search"])
async def test_remote_workspace_inventory_uses_resource_relay(history_context, authority, operation):
    arguments = {
        "resource_identity": str(workspace_session_inventory_identity(REMOTE_WORKSPACE)),
        "uri": workspace_session_inventory_uri(REMOTE_WORKSPACE, gateway_authority=authority),
        "scope": "workspace",
    }
    if operation == "search":
        arguments["query"] = "ALPHA"
        tool = create_search_context_tool(history_context)
    else:
        arguments["view"] = "inventory"
        tool = create_read_context_tool(history_context)
    result = json.loads(await tool.ainvoke(arguments))
    assert result["uri"] == arguments["uri"]
    name, request = history_context.workspace_session_context_client.calls[0]
    assert name == f"resource-{operation}"
    assert request.uri == arguments["uri"]
    assert request.resource_identity == arguments["resource_identity"]
    assert history_context.session_context_query_service.calls == []


@pytest.mark.parametrize("remote", [False, True])
async def test_gateway_context_search_uses_shared_query_and_preserves_target(history_context, remote):
    origin, target = "gateway_" + "a" * 32, "gateway_" + "b" * 32 if remote else "gateway_" + "a" * 32
    token = set_current_gateway_id(origin)
    try:
        uri = gateway_session_inventory_uri(target, gateway_authority=target if remote else None)
        result = json.loads(await create_search_context_tool(history_context).ainvoke({
            "resource_identity": str(gateway_session_inventory_identity(target)), "uri": uri, "scope": "gateway",
            "query": "needle", "sources": ["session_information"], "match_mode": "regex",
            "case_sensitive": True, "max_results": 3, "max_chars": 2048,
        }))
    finally:
        reset_current_gateway_id(token)
    assert result == {"events": []}
    operation, query = history_context.workspace_session_context_client.calls[0]
    assert operation == "gateway-search"
    assert query.source_gateway_id == origin and query.resource_target.gateway_id == target
    assert query.resource_target.uri == uri
    assert query.resource_search.scope.mode == "all" and query.resource_search.max_results == 3
    domain, = query.resource_search.queries
    assert (domain.category, domain.operation, domain.match_mode, domain.limit) == ("context", "content", "regex", 3)
    assert domain.context.sources == ("session_information",) and domain.context.case_sensitive and domain.context.max_chars == 2048
    assert history_context.session_context_query_service.calls == []


@pytest.mark.parametrize("field,value", [("cursor", "old-offset"), ("revision", "old-global-revision")])
async def test_gateway_context_search_rejects_global_paging(history_context, field, value):
    tool = create_search_context_tool(history_context)
    token = set_current_gateway_id("gateway_" + "a" * 32)
    try:
        result = await tool.ainvoke({"resource_identity": str(gateway_session_inventory_identity("gateway_" + "a" * 32)),
            "uri": gateway_session_inventory_uri("gateway_" + "a" * 32), "scope": "gateway", "query": "needle", field: value})
    finally:
        reset_current_gateway_id(token)
    assert "continuations" in result and history_context.workspace_session_context_client.calls == []
