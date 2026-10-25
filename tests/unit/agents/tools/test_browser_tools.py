from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.browser import (
    OpenBrowserPageInput,
    RunPlaywrightCodeInput,
    create_list_browser_page_tool,
    create_navigate_page_tool,
    create_open_browser_page_tool,
    create_read_page_tool,
    create_run_playwright_code_tool,
    create_screenshot_page_tool,
)
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.core.turn_execution_scope import (
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import (
    CHILD_THREAD_A,
    SESSION_A,
    THREAD_A,
    WORKSPACE_ID,
)


@pytest.fixture
def browser_owner_fixture(tmp_path: Path) -> BrowserOwnerFixture:
    return build_browser_owner_fixture(tmp_path)


@pytest.fixture
def browser_tool_context(browser_owner_fixture: BrowserOwnerFixture) -> Iterator[SimpleNamespace]:
    invocation = ToolInvocationContext(thread_binding=ThreadRuntimeBinding(SESSION_A, THREAD_A))
    token = invocation.set_tool_call_id("tool-browser-test")
    scope_token = set_current_turn_execution_scope(TurnExecutionScope("execution-browser-test"))
    admission = ExecutionAdmission(
        session_id=SESSION_A, thread_id=THREAD_A, execution_id="execution-browser-test",
        job_id="job-browser-test", admission_idempotency_key="admission-browser-test",
        execution_origin="internal", turn_id=None,
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(SESSION_A, THREAD_A), langgraph_thread_id=THREAD_A),
        execution_admission_scope(admission, langgraph_thread_id=THREAD_A),
    ):
        try:
            yield SimpleNamespace(session_id=SESSION_A, browser_resource_owner=browser_owner_fixture.owner,
                                  invocation_context=invocation)
        finally:
            invocation.reset_tool_call_id(token)
            reset_current_turn_execution_scope(scope_token)


def test_browser_schema_keeps_default_timeout_and_explicit_retention_scope() -> None:
    assert RunPlaywrightCodeInput.model_fields["timeoutMs"].default == 10000
    scope = OpenBrowserPageInput.model_json_schema()["properties"]["retention_scope"]
    assert scope["default"] == "thread"
    assert scope["enum"] == ["thread", "workspace"]


@pytest.mark.asyncio
async def test_run_playwright_code_forwards_default_timeout_with_real_owner_lease(
    browser_owner_fixture: BrowserOwnerFixture, browser_tool_context: SimpleNamespace,
) -> None:
    browser = await browser_owner_fixture.create()
    tool = create_run_playwright_code_tool(browser_tool_context)
    result = json.loads(await tool.ainvoke({"pageId": browser["resource_id"], "code": "return 'ok';"}))
    assert result["result"] == "ok"
    assert browser_owner_fixture.transport.calls[-1] == (
        browser["resource_id"], "run", {"code": "return 'ok';", "timeoutMs": 10000}, "agent",
    )
    assert browser_owner_fixture.leases.leases_for_resource(browser["resource_id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("factory,payload", [
    (create_navigate_page_tool, {"type": "url", "url": "https://example.test"}),
    (create_read_page_tool, {}),
])
async def test_page_results_separate_browser_identity_from_internal_tabs(
    browser_owner_fixture: BrowserOwnerFixture, browser_tool_context: SimpleNamespace,
    factory, payload: dict[str, object],
) -> None:
    browser = await browser_owner_fixture.create()
    browser_resource_id = browser["resource_id"]
    perform = browser_owner_fixture.transport.perform
    async def envelope(*args: object, **kwargs: object) -> dict[str, object]:
        result = await perform(*args, **kwargs)
        return {**result["browser"], **result}
    browser_owner_fixture.transport.perform = envelope
    result = json.loads(
        await factory(browser_tool_context).ainvoke(
            {"pageId": browser_resource_id, **payload}
        )
    )
    assert result["pageId"] == result["browserId"] == browser_resource_id
    assert result["activePageId"] == "page_internal"
    assert result["pages"][0]["tabId"] == "page_internal"
    assert "page_id" not in result
    assert "active_page_id" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("factory,arguments,action", [
    (create_run_playwright_code_tool, {"code": "return 1;"}, "run"),
    (create_read_page_tool, {}, "read"),
    (create_screenshot_page_tool, {}, "screenshot"),
])
async def test_retryable_timeout_remains_structured_and_owner_unknown_is_visible(
    browser_owner_fixture: BrowserOwnerFixture, browser_tool_context: SimpleNamespace,
    factory, arguments: dict[str, object], action: str,
) -> None:
    browser = await browser_owner_fixture.create()
    browser_owner_fixture.transport.failure = BrowserRequestError(method="POST", path=f"/{action}", status=408, payload={
        "code": "browser_tool_timeout", "error": "动作超时", "retryable": True,
        "recovery": "page_reset", "timeout_ms": 15000,
    })
    result = json.loads(await factory(browser_tool_context).ainvoke({"pageId": browser["resource_id"], **arguments}))
    assert result["status"] == "error"
    assert result["retryable"] is True
    assert result["timeoutMs"] == 15000
    assert result["code"] == "browser_tool_timeout"
    assert (await browser_owner_fixture.owner.get_resource(browser["resource_id"]))["reconciliation_status"] == "unknown"


@pytest.mark.asyncio
async def test_transport_timeout_uses_real_client_error_contract(
    browser_owner_fixture: BrowserOwnerFixture, browser_tool_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch, resource_http,
) -> None:
    browser = await browser_owner_fixture.create()
    client = BrowserManagerClient(workspace_id=WORKSPACE_ID, backend_url="http://browser.test")
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)
    resource_http(timeout)
    monkeypatch.setattr(browser_owner_fixture.transport, "perform", client.perform)
    result = json.loads(await create_read_page_tool(browser_tool_context).ainvoke({"pageId": browser["resource_id"]}))
    assert result["code"] == "browser_tool_timeout"
    assert result["timeoutMs"] == 30000
    assert result["recovery"] == "page_reset"


@pytest.mark.asyncio
async def test_list_is_exact_thread_read_only_and_summary_contains_no_paths(
    browser_owner_fixture: BrowserOwnerFixture, browser_tool_context: SimpleNamespace,
) -> None:
    browser = await browser_owner_fixture.create()
    await browser_owner_fixture.create(thread=CHILD_THREAD_A)
    result = json.loads(await create_list_browser_page_tool(browser_tool_context).ainvoke({}))
    assert result["count"] == 1
    assert result["pages"][0]["pageId"] == browser["resource_id"]
    assert "workspace_root" not in json.dumps(result)
    assert "checkpoint" not in json.dumps(result)
    assert "attach_url" not in json.dumps(result)
    assert browser_owner_fixture.transport.calls == []


@pytest.mark.asyncio
async def test_open_reuses_only_current_thread_and_explicit_workspace_promotes(
    browser_owner_fixture: BrowserOwnerFixture, browser_tool_context: SimpleNamespace,
) -> None:
    browser = await browser_owner_fixture.create()
    await browser_owner_fixture.create(thread=CHILD_THREAD_A)
    result = json.loads(await create_open_browser_page_tool(browser_tool_context).ainvoke({
        "url": "https://example.test", "retention_scope": "workspace",
    }))
    assert result["pageId"] == browser["resource_id"]
    assert result["reused"] is True
    assert result["retention_scope"] == "workspace"
    assert browser_owner_fixture.transport.creates == 2
    assert [call[1] for call in browser_owner_fixture.transport.calls] == ["navigate", "read"]


@pytest.mark.asyncio
async def test_tool_rejects_missing_runtime_binding_before_any_side_effect(
    browser_owner_fixture: BrowserOwnerFixture,
) -> None:
    unbound_context = SimpleNamespace(session_id=SESSION_A, browser_resource_owner=browser_owner_fixture.owner,
                              invocation_context=ToolInvocationContext())
    with pytest.raises(RuntimeError, match="真实 ThreadRuntimeBinding"):
        await create_open_browser_page_tool(unbound_context).ainvoke({"url": "about:blank"})
    assert browser_owner_fixture.transport.creates == 0
