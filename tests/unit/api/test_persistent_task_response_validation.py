"""持久任务管理的内部校验失败不能被请求错误出口吞掉。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.abstractions.state_conflict import ClientStateConflictError
from app.api import persistent_tasks
from app.api.deps import get_persistent_task_service, verify_local_token
from app.core.exceptions import NotFoundError
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskCheckRequest,
    PersistentTaskCreateRequest,
    PersistentTaskRunDTO,
)
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

REQUEST_ID = "req-task-response-validation"
BASE = "/api/v1/persistent-tasks"
NATIVE_PATHS = [
    "/private/task-api-sentinel",
    r"C:\private\task-api-sentinel",
    r"\\target\private\task-api-sentinel",
]


def _configuration(workspace_id: str) -> dict[str, object]:
    return {
        "name": "校验测试",
        "trigger": {"type_id": "builtin.manual", "version": "1"},
        "condition": {"type_id": "builtin.always", "version": "1"},
        "context": {"type_id": "builtin.literal", "version": "1", "content": ""},
        "action": {
            "type_id": "builtin.session_generation",
            "version": "1",
            "generation": {
                "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"},
                "config": {"prompt": "执行测试任务"},
                "placement": {"kind": "workspace", "workspace_id": workspace_id},
                "context_source": {"kind": "fresh"},
                "session_strategy": {"mode": "new_per_run"},
            },
        },
    }


CONFIGURATION = _configuration("workspace-validation")
ASYNC_CASES = [
    pytest.param(
        "POST",
        "",
        {"idempotency_key": "create", "configuration": CONFIGURATION},
        "create",
        id="create",
    ),
    pytest.param(
        "PUT",
        "/task",
        {"expected_revision": 1, "configuration": CONFIGURATION},
        "update",
        id="update",
    ),
    pytest.param(
        "POST",
        "/task/control",
        {"expected_revision": 1, "action": "pause"},
        "control",
        id="control",
    ),
    pytest.param(
        "POST",
        "/task/checks",
        {"idempotency_key": "check", "expected_revision": 1},
        "check",
        id="check",
    ),
    pytest.param(
        "POST",
        "/task/notifications",
        {"idempotency_key": "notify", "payload": {}},
        "check",
        id="notify",
    ),
    pytest.param("GET", "/task/runs/run", None, "inspect", id="inspect"),
    pytest.param(
        "POST",
        "/imports/session-generators",
        {
            "task_id": "old-generator",
            "source_hash": "a" * 64,
            "configuration": CONFIGURATION,
            "revision": 1,
            "lifecycle": "active",
            "created_at": "2026-10-31T00:00:00Z",
            "updated_at": "2026-10-31T00:00:00Z",
            "workspace_ids": {},
            "runs": [],
        },
        "import",
        id="import",
    ),
]
CASES = [*ASYNC_CASES, pytest.param("GET", "/task/runs", None, "runs", id="runs")]


@dataclass
class TaskBoundaryService:
    error: Exception = field(default_factory=lambda: ValueError("持久任务输入无效"))
    calls: list[str] = field(default_factory=list)
    runtime: object = field(default_factory=object)

    def _fail(self, operation: str) -> NoReturn:
        self.calls.append(operation)
        raise self.error

    async def create(self, request: object) -> NoReturn:
        self._fail("create")

    async def update(self, task_id: str, request: object) -> NoReturn:
        self._fail("update")

    async def control(self, task_id: str, request: object) -> NoReturn:
        self._fail("control")

    async def check(self, task_id: str, request: object) -> NoReturn:
        self._fail("check")

    def runs(self, task_id: str) -> NoReturn:
        self._fail("runs")

    async def inspect_run(self, task_id: str, run_id: str) -> NoReturn:
        self._fail("inspect")


@pytest.fixture
def boundary_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, TaskBoundaryService]]:
    from app.main import app as workspace_app

    service = TaskBoundaryService()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(persistent_tasks.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(
            error_type, workspace_app.exception_handlers[error_type]
        )
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_persistent_task_service] = lambda: service

    async def fail_import(runtime: object, request: object) -> NoReturn:
        service._fail("import")

    monkeypatch.setattr(persistent_tasks, "import_generator", fail_import)
    with TestClient(
        application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}
    ) as client:
        yield client, service


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), CASES)
@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_internal_validation_remains_a_server_failure(
    boundary_client: tuple[TestClient, TaskBoundaryService],
    method: str,
    suffix: str,
    payload: dict[str, object] | None,
    operation: str,
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = boundary_client
    with pytest.raises(ValidationError) as captured:
        PersistentTaskRunDTO.model_validate(
            {
                "run_id": "run",
                "task_id": "task",
                "task_revision": 1,
                "definition_revision": 1,
                "trigger_key": "trigger",
                "check_only": False,
                "phase": native,
                "created_at": "2026-10-31T00:00:00Z",
                "updated_at": "2026-10-31T00:00:00Z",
            }
        )
    service.error = captured.value
    response = client.request(method, BASE + suffix, json=payload)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500,
        "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None,
        "request_id": REQUEST_ID,
    }
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert (
        "task-api-sentinel" not in response.text and "input_value" not in response.text
    )
    assert service.calls == [operation]
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and record.exc_info[1] is service.error
    ]
    assert len(diagnostics) == 1 and "task-api-sentinel" in caplog.text


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), CASES)
@pytest.mark.parametrize(
    ("error", "status"),
    [(ValueError("请求被拒绝"), 400), (KeyError("请求被拒绝"), 404)],
)
def test_business_input_and_lookup_rejection_keep_existing_contract(
    boundary_client: tuple[TestClient, TaskBoundaryService],
    method: str,
    suffix: str,
    payload: dict[str, object] | None,
    operation: str,
    error: Exception,
    status: int,
) -> None:
    client, service = boundary_client
    service.error = error
    response = client.request(method, BASE + suffix, json=payload)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": "请求被拒绝", "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID and service.calls == [
        operation
    ]


@pytest.mark.parametrize(("method", "suffix", "payload", "operation"), ASYNC_CASES)
@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (ClientStateConflictError("任务状态冲突"), 409, "任务状态冲突"),
        (NotFoundError(), 404, "resource not found"),
    ],
)
def test_named_async_business_rejection_keeps_existing_contract(
    boundary_client: tuple[TestClient, TaskBoundaryService],
    method: str,
    suffix: str,
    payload: dict[str, object] | None,
    operation: str,
    error: Exception,
    status: int,
    detail: str,
) -> None:
    client, service = boundary_client
    service.error = error
    response = client.request(method, BASE + suffix, json=payload)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail, "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID and service.calls == [
        operation
    ]


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [
        ("", {"idempotency_key": "", "configuration": CONFIGURATION}),
        ("/task/control", {"expected_revision": 0, "action": "pause"}),
        ("/task/notifications", {"idempotency_key": "notify", "payload": []}),
        ("/imports/session-generators", {}),
    ],
)
def test_request_validation_precedes_service_access(
    boundary_client: tuple[TestClient, TaskBoundaryService],
    suffix: str,
    payload: dict[str, object],
) -> None:
    client, service = boundary_client
    response = client.post(BASE + suffix, json=payload)
    assert response.status_code == 422, response.text
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    assert service.calls == []


@pytest.fixture
def stored_run(
    boundary_client: tuple[TestClient, TaskBoundaryService],
    empty_workspace_scope: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, WorkspaceStateStore, PersistentTaskRunDTO]]:
    client, service = boundary_client
    workspace_id, workspace_root = empty_workspace_scope
    with WorkspaceStateStore(workspace_root=workspace_root) as state:
        store = PersistentTaskStore(state, workspace_id=workspace_id)
        now = datetime(2026, 10, 31, tzinfo=UTC)
        task = store.create(
            PersistentTaskCreateRequest.model_validate(
                {
                    "idempotency_key": "create",
                    "configuration": _configuration(workspace_id),
                }
            ),
            now,
        )
        original = store.trigger(
            task.task_id,
            PersistentTaskCheckRequest(idempotency_key="check", expected_revision=1),
            now,
            request_id=REQUEST_ID,
        )
        monkeypatch.setattr(service, "runs", store.runs)
        yield client, state, original


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_real_store_validation_preserves_run_and_recovers_after_repair(
    stored_run: tuple[TestClient, WorkspaceStateStore, PersistentTaskRunDTO],
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, state, original = stored_run
    with closing(state.connection()) as connection:
        connection.execute(
            "UPDATE persistent_task_runs SET decision_json = ? WHERE run_id = ?",
            (
                json.dumps(
                    {"decision": native, "reason": "损坏的内部结果", "result": {}}
                ),
                original.run_id,
            ),
        )
        before = tuple(
            connection.execute(
                "SELECT * FROM persistent_task_runs WHERE run_id = ?",
                (original.run_id,),
            ).fetchone()
        )
        task_before = tuple(
            connection.execute(
                "SELECT * FROM persistent_tasks WHERE task_id = ?", (original.task_id,)
            ).fetchone()
        )
    response = client.get(BASE + f"/{original.task_id}/runs")
    assert response.status_code == 500, response.text
    assert (
        response.json()["message"]
        == "请求处理失败: stage=http_request, error=ValidationError"
    )
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    assert (
        "task-api-sentinel" not in response.text and "task-api-sentinel" in caplog.text
    )
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and isinstance(record.exc_info[1], ValidationError)
    ]
    assert len(diagnostics) == 1
    failure = diagnostics[0].exc_info
    assert failure is not None and isinstance(failure[1], ValidationError)
    assert failure[1].errors()[0]["input"] == native
    with closing(state.connection()) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM persistent_task_runs WHERE run_id = ?",
                    (original.run_id,),
                ).fetchone()
            )
            == before
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM persistent_tasks WHERE task_id = ?",
                    (original.task_id,),
                ).fetchone()
            )
            == task_before
        )
        connection.execute(
            "UPDATE persistent_task_runs SET decision_json = NULL WHERE run_id = ?",
            (original.run_id,),
        )
    repaired = client.get(BASE + f"/{original.task_id}/runs")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"] == {"items": [original.model_dump(mode="json")]}
    assert (
        repaired.json()["request_id"] == repaired.headers["X-Request-ID"] == REQUEST_ID
    )
