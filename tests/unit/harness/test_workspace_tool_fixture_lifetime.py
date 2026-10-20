from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from tests.support import workspace_tool_runtime as runtime_fixture

CLEANUP_ORDER = [
    "call-reset", "scope-reset", "scope-close", "browser", "terminal",
    "lifespan-exit", "activity", "catalog",
]


@dataclass
class FixtureLifetime:
    application: FastAPI
    registry: Mock
    context: Mock
    container: SimpleNamespace
    events: list[str]
    failures: dict[str, BaseException]


@pytest.fixture
def fixture_lifetime(monkeypatch: pytest.MonkeyPatch) -> FixtureLifetime:
    events: list[str] = []
    failures: dict[str, BaseException] = {}
    admission_active = False

    def cleanup(label: str) -> None:
        events.append(label)
        assert admission_active == (label in CLEANUP_ORDER[:5])
        if label in failures:
            raise failures[label]

    @contextmanager
    def admission_scope(*_args, **_kwargs) -> Iterator[None]:
        nonlocal admission_active
        admission_active = True
        try:
            yield
        finally:
            admission_active = False

    @asynccontextmanager
    async def workspace_lifespan(_container) -> AsyncIterator[None]:
        if "startup" in failures:
            raise failures["startup"]
        try:
            yield
        finally:
            cleanup("lifespan-exit")

    session = SimpleNamespace(session_id="sess_00000000000070008000000000000001")
    admission = SimpleNamespace(execution_id="exec_00000000000070008000000000000002")
    container = SimpleNamespace(
        session_service=SimpleNamespace(create=AsyncMock(return_value=session)),
        session_catalog=SimpleNamespace(
            resolver=SimpleNamespace(main_thread_id=Mock(return_value="thr_00000000000070008000000000000003")),
            close=Mock(side_effect=lambda: cleanup("catalog")),
        ),
        checkpointer=SimpleNamespace(
            accept_turn=Mock(return_value={"execution_id": admission.execution_id}),
            execution_admission=Mock(return_value=admission),
        ),
        browser_resource_owner=SimpleNamespace(cleanup_session=AsyncMock(side_effect=lambda _id: cleanup("browser"))),
        terminal_resource_owner=SimpleNamespace(cleanup_session=AsyncMock(side_effect=lambda _id: cleanup("terminal"))),
        workspace_activity_service=SimpleNamespace(store=SimpleNamespace(close=Mock(side_effect=lambda: cleanup("activity")))),
    )
    registry = Mock()
    registry.get.return_value = container
    context = Mock(spec=runtime_fixture.ToolInvocationContext)
    context.reset_tool_call_id.side_effect = lambda _token: cleanup("call-reset")
    scope = SimpleNamespace(close=AsyncMock(side_effect=lambda: cleanup("scope-close")))
    monkeypatch.setattr(runtime_fixture, "load_or_create_workspace_id", lambda _root: "00000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(runtime_fixture, "WorkspaceContainerRegistry", lambda **_kwargs: nullcontext(registry))
    monkeypatch.setattr(runtime_fixture, "catalog_provider_scope", lambda _registry: nullcontext())
    monkeypatch.setattr(runtime_fixture, "_workspace_lifespan", workspace_lifespan)
    monkeypatch.setattr(runtime_fixture, "ToolInvocationContext", lambda **_kwargs: context)
    monkeypatch.setattr(runtime_fixture, "session_thread_owner_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(runtime_fixture, "TurnExecutionScope", lambda _execution_id: scope)
    monkeypatch.setattr(runtime_fixture, "set_current_turn_execution_scope", lambda _scope: object())
    monkeypatch.setattr(runtime_fixture, "reset_current_turn_execution_scope", lambda _token: cleanup("scope-reset"))
    monkeypatch.setattr(runtime_fixture, "execution_admission_scope", admission_scope)
    return FixtureLifetime(FastAPI(), registry, context, container, events, failures)


async def test_successful_teardown_keeps_cleanup_order_and_releases_application_binding(
    fixture_lifetime: FixtureLifetime, tmp_path: Path,
) -> None:
    state = fixture_lifetime
    async with runtime_fixture.workspace_tool_runtime(tmp_path, application=state.application) as runtime:
        assert runtime == (state.container, "sess_00000000000070008000000000000001", state.context)
        assert state.application.state.workspace_container_registry is state.registry

    assert state.events == CLEANUP_ORDER
    assert state.application.state.workspace_container_registry is None


@pytest.mark.parametrize("stage", [label for label in CLEANUP_ORDER if label != "lifespan-exit"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_cleanup_failure_or_cancellation_still_attempts_all_remaining_resources(
    fixture_lifetime: FixtureLifetime, tmp_path: Path, stage: str, cancelled: bool,
) -> None:
    state = fixture_lifetime
    failure = asyncio.CancelledError("装具清理取消") if cancelled else OSError("装具清理失败")
    state.failures[stage] = failure

    with pytest.raises(type(failure)) as caught:
        async with runtime_fixture.workspace_tool_runtime(tmp_path, application=state.application):
            pass

    assert caught.value is failure
    assert state.events == CLEANUP_ORDER
    assert state.application.state.workspace_container_registry is None


@pytest.mark.parametrize("setup_fails", [False, True])
async def test_multiple_cleanup_failures_preserve_body_or_startup_error_chain(
    fixture_lifetime: FixtureLifetime, tmp_path: Path, setup_fails: bool,
) -> None:
    state = fixture_lifetime
    original = LookupError("装具准备或主体失败")
    activity_error = OSError("活动数据库关闭失败")
    catalog_error = ValueError("catalog关闭失败")
    state.failures.update(activity=activity_error, catalog=catalog_error)
    if setup_fails:
        state.failures["startup"] = original

    with pytest.raises(ValueError) as caught:
        async with runtime_fixture.workspace_tool_runtime(tmp_path, application=state.application):
            raise original

    assert caught.value is catalog_error
    assert catalog_error.__context__ is activity_error
    assert activity_error.__context__ is original
    assert state.events == (["activity", "catalog"] if setup_fails else CLEANUP_ORDER)
    assert state.application.state.workspace_container_registry is None
