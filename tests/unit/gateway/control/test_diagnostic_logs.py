from __future__ import annotations

import base64
import json

import pytest
from pydantic import ValidationError

from app.core.logging_contract import get_runtime_log_root, load_runtime_log_contract
from app.gateway.control.diagnostic_logs import (
    locate_diagnostic_log,
    query_diagnostic_logs,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.log_query import RuntimeLogQueryError
from app.schemas.gateway.diagnostics import (
    GatewayDiagnosticLogLocateRequest,
    GatewayDiagnosticLogQueryRequest,
    GatewayDiagnosticLogQueryScopeDTO,
)

GATEWAY_ID = "gateway_00000000000040008000000000000001"
WORKSPACE_A = "00000000-0000-4000-8000-000000000001"
WORKSPACE_B = "00000000-0000-4000-8000-000000000002"
WORKSPACE_SHARED = "00000000-0000-4000-8000-000000000003"


def request_for(*workspace_ids: str, query: str = "sharedword", limit: int = 50, cursor: str | None = None) -> GatewayDiagnosticLogQueryRequest:
    return GatewayDiagnosticLogQueryRequest(
        scope=GatewayDiagnosticLogQueryScopeDTO(source="workspace" if workspace_ids else "gateway", workspace_ids=workspace_ids),
        query=query, limit=limit, cursor=cursor,
    )


async def test_workspace_sources_are_explicit_and_shared_backend_is_read_once(diagnostic_registry: GatewayWorkspaceRegistry) -> None:
    page = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, WORKSPACE_SHARED), gateway_id=GATEWAY_ID)
    assert len(page.items) == 1
    assert page.items[0].source.workspace_ids == [WORKSPACE_A, WORKSPACE_SHARED]
    assert page.items[0].source.source == "workspace" and "only-a" in page.items[0].preview
    assert page.scanned_lines == 1
    assert all(WORKSPACE_B not in source.workspace_ids and source.source != "gateway" for source in page.sources)
    assert not page.complete and page.limited_reason == "source_unavailable"
    other = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_B), gateway_id=GATEWAY_ID)
    assert len(other.items) == 1 and "only-b" in other.items[0].preview


async def test_gateway_query_excludes_registered_workspace_files(diagnostic_registry: GatewayWorkspaceRegistry) -> None:
    page = await query_diagnostic_logs(diagnostic_registry, request_for(), gateway_id=GATEWAY_ID)
    assert {item.preview for item in page.items} == {"only-g sharedword", "launcher sharedword"}
    assert all(source.source == "gateway" and not source.workspace_ids for source in page.sources)
    assert page.complete and page.cursor is None


async def test_unavailable_source_before_continuation_prevents_false_complete(diagnostic_registry: GatewayWorkspaceRegistry) -> None:
    first = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1), gateway_id=GATEWAY_ID)
    assert any(source.status == "unavailable" for source in first.sources)
    while first.cursor is not None:
        first = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1, cursor=first.cursor), gateway_id=GATEWAY_ID)
    assert not first.complete and first.limited_reason == "source_unavailable"


async def test_owner_cursor_continues_same_unicode_line_and_exact_read_keeps_scope(diagnostic_registry: GatewayWorkspaceRegistry) -> None:
    first = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1), gateway_id=GATEWAY_ID)
    assert first.cursor is not None and first.limited_reason == "result_limit"
    second = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1, cursor=first.cursor), gateway_id=GATEWAY_ID)
    assert len(second.items) == 1 and second.items[0].reference.start_codepoint > first.items[0].reference.start_codepoint
    located = await locate_diagnostic_log(diagnostic_registry, GatewayDiagnosticLogLocateRequest(
        scope=first.scope, reference=first.items[0].reference,
    ), gateway_id=GATEWAY_ID)
    ref = located.reference
    assert located.text[ref.start_codepoint:ref.end_codepoint] == "needle"
    with pytest.raises(RuntimeLogQueryError) as caught:
        await locate_diagnostic_log(diagnostic_registry, GatewayDiagnosticLogLocateRequest(
            scope=request_for(WORKSPACE_B).scope, reference=ref,
        ), gateway_id=GATEWAY_ID)
    assert caught.value.reason == "source_unavailable"


@pytest.mark.parametrize("change,reason", [("owner", "owner_mismatch"), ("scope", "scope_mismatch"), ("query", "query_mismatch"), ("offset", "position_out_of_bounds"), ("malformed", "malformed")])
async def test_cursor_rejection_preserves_machine_reason(diagnostic_registry: GatewayWorkspaceRegistry, change: str, reason: str) -> None:
    first = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1), gateway_id=GATEWAY_ID)
    assert first.cursor is not None
    payload = json.loads(base64.urlsafe_b64decode(first.cursor + "=" * (-len(first.cursor) % 4)))
    if change == "owner":
        payload["gateway_id"] = "gateway_other"
    elif change == "scope":
        payload["scope"] = {"source": "workspace", "workspace_ids": [WORKSPACE_B]}
    elif change == "query":
        payload["query"] = "other"
    elif change == "offset":
        payload["source_offset"] = 1000
    cursor = "not-base64" if change == "malformed" else base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(RuntimeLogQueryError) as caught:
        await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1, cursor=cursor), gateway_id=GATEWAY_ID)
    assert (caught.value.code, caught.value.reason) == ("invalid_runtime_log_cursor", reason)


async def test_append_rejects_owner_cursor_without_breaking_unchanged_hit(diagnostic_registry: GatewayWorkspaceRegistry) -> None:
    first = await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1), gateway_id=GATEWAY_ID)
    path = load_runtime_log_contract().path_for(get_runtime_log_root(), "backend", 41001)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("appended\n")
    with pytest.raises(RuntimeLogQueryError) as caught:
        await query_diagnostic_logs(diagnostic_registry, request_for(WORKSPACE_A, query="needle", limit=1, cursor=first.cursor), gateway_id=GATEWAY_ID)
    assert caught.value.reason == "file_changed"
    assert (await locate_diagnostic_log(diagnostic_registry, GatewayDiagnosticLogLocateRequest(
        scope=first.scope, reference=first.items[0].reference,
    ), gateway_id=GATEWAY_ID)).text == "only-a sharedword 😀needle needle"


def test_scope_and_query_cannot_expand_implicitly() -> None:
    with pytest.raises(ValidationError):
        GatewayDiagnosticLogQueryScopeDTO(source="workspace")
    with pytest.raises(ValidationError):
        GatewayDiagnosticLogQueryScopeDTO(source="gateway", workspace_ids=(WORKSPACE_A,))
    with pytest.raises(ValidationError):
        request_for(query="multiple\nlines")
