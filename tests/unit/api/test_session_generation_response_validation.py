"""生成器的用户命名拒绝与内部响应损坏必须分属400和500。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import NoReturn
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import persistent_tasks, session_navigation
from app.api.deps import (
    get_persistent_task_service,
    get_session_generation_service,
    verify_local_token,
)
from app.core import failures
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationPreviewDTO,
)
from app.services.business.persistent_task_service import PersistentTaskService
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_generation.providers import (
    AgentPromptGenerationProvider,
)
from app.services.business.session_generation.service import SessionGenerationService
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_service import SessionService
from app.services.orchestration.persistent_tasks.runtime import PersistentTaskRuntime

WORKSPACE_ID = "workspace-validation"
REQUEST_ID = "req-generation-validation"
BASE = "/api/v1/session-generations"
NATIVE_PATHS = ["/private/gen-secret", r"C:\x\gen-secret", r"\\target\x\gen-secret"]
PREVIEW = {"name": "校验任务", "title_template": "{generator.name}"}


def _definition() -> dict[str, object]:
    return {
        "name": "校验任务",
        "title_template": "{generator.name}",
        "path_template": [],
        "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"},
        "config": {"prompt": "继续执行"},
        "placement": {"kind": "workspace", "workspace_id": WORKSPACE_ID},
        "execution_workspace_id": WORKSPACE_ID,
        "context_source": {"kind": "fresh"},
        "session_strategy": {"mode": "new_per_run"},
    }


def _task_configuration(definition: dict[str, object]) -> dict[str, object]:
    return {
        "name": definition["name"],
        "trigger": {"type_id": "builtin.manual", "version": "1"},
        "condition": {"type_id": "builtin.always", "version": "1"},
        "context": {"type_id": "builtin.literal", "version": "1", "content": ""},
        "action": {
            "type_id": "builtin.session_generation",
            "version": "1",
            "generation": {
                key: value
                for key, value in definition.items()
                if key not in {"name", "execution_workspace_id"}
            },
        },
    }


@dataclass
class GenerationCase:
    client: TestClient
    generation: SessionGenerationService
    runtime: Mock
    catalog: Mock


@pytest.fixture
def generation_case() -> Iterator[GenerationCase]:
    from app.main import app as workspace_app

    sessions = Mock(spec=SessionService, workspace_id=WORKSPACE_ID)
    catalog = Mock(spec=SessionCatalogService, workspace_id=WORKSPACE_ID)
    generation = SessionGenerationService(
        session_service=sessions,
        session_catalog_service=catalog,
        session_context_fork_service=Mock(spec=SessionContextForkService),
        providers=[AgentPromptGenerationProvider()],
    )
    runtime = Mock(spec=PersistentTaskRuntime, store=Mock(), generation=generation)
    tasks = PersistentTaskService(runtime=runtime, sessions=sessions)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(session_navigation.router, prefix="/api/v1")
    application.include_router(persistent_tasks.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(
            error_type, workspace_app.exception_handlers[error_type]
        )
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_session_generation_service] = lambda: (
        generation
    )
    application.dependency_overrides[get_persistent_task_service] = lambda: tasks
    with TestClient(
        application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}
    ) as client:
        yield GenerationCase(client, generation, runtime, catalog)


@pytest.mark.parametrize(
    ("suffix", "method", "payload"),
    [
        ("/validate-definition", "validate_definition", _definition()),
        ("/preview-placement", "preview", PREVIEW),
        ("/validate-definition", "preview", _definition()),
    ],
)
@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_internal_response_validation_uses_server_failure(
    generation_case: GenerationCase,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    method: str,
    payload: dict[str, object],
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ValidationError) as captured:
        SessionGenerationPreviewDTO.model_validate(
            {
                "workspace_id": {native: native},
                "title": "校验任务",
                "path_segments": [],
                "navigation_path": ["校验任务"],
                "physical_location": "unallocated",
            }
        )
    failure = captured.value

    async def fail(request: object) -> NoReturn:
        raise failure

    monkeypatch.setattr(generation_case.generation, method, fail)
    response = generation_case.client.post(BASE + suffix, json=payload)
    assert response.status_code == 500, response.text
    assert response.json() == {
        "code": 500,
        "message": "请求处理失败: stage=http_request, error=ValidationError",
        "data": None,
        "request_id": REQUEST_ID,
    }
    assert (
        response.headers["X-Request-ID"] == REQUEST_ID
        and "gen-secret" not in response.text
    )
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and record.exc_info[1] is failure
    ]
    assert len(diagnostics) == 1 and "gen-secret" in caplog.text


@pytest.mark.parametrize("native", NATIVE_PATHS)
def test_real_preview_response_validation_can_be_retried_after_metadata_repair(
    generation_case: GenerationCase,
    native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = generation_case
    case.catalog.workspace_id = {native: native}
    response = case.client.post(BASE + "/preview-placement", json=PREVIEW)
    assert response.status_code == 500, response.text
    assert (
        response.json()["message"]
        == "请求处理失败: stage=http_request, error=ValidationError"
    )
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    assert "gen-secret" not in response.text and "gen-secret" in caplog.text
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and isinstance(record.exc_info[1], ValidationError)
    ]
    assert len(diagnostics) == 1
    case.catalog.workspace_id = WORKSPACE_ID
    repaired = case.client.post(BASE + "/preview-placement", json=PREVIEW)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"] == {
        "workspace_id": WORKSPACE_ID,
        "title": "校验任务",
        "path_segments": [],
        "navigation_path": ["校验任务"],
        "physical_location": "unallocated",
        "target_session_id": None,
    }
    assert (
        repaired.json()["request_id"] == repaired.headers["X-Request-ID"] == REQUEST_ID
    )


@pytest.mark.parametrize(
    ("field", "rule", "value"),
    [
        *(
            pytest.param(
                "name", "string_too_long", native * 40, id="name-" + str(index)
            )
            for index, native in enumerate(NATIVE_PATHS)
        ),
        *(
            pytest.param(
                "session_title",
                "string_too_long",
                native * 40,
                id="title-" + str(index),
            )
            for index, native in enumerate(NATIVE_PATHS)
        ),
        pytest.param("name", "string_too_short", "", id="empty-name"),
    ],
)
def test_deferred_user_naming_validation_keeps_400_without_input_echo(
    generation_case: GenerationCase,
    field: str,
    rule: str,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    definition = _definition()
    if field == "name":
        definition["name"] = value
    else:
        definition["config"] = {"prompt": "继续执行", "session_title": value}
    response = generation_case.client.post(
        BASE + "/validate-definition", json=definition
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "stage=session-generation-naming, error=ValidationError" in detail
    assert (
        f"{field}:{rule}" in detail
        and "input_value" not in detail
        and "gen-secret" not in detail
    )
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    diagnostics = [
        record
        for record in caplog.records
        if record.exc_info and isinstance(record.exc_info[1], ValidationError)
    ]
    assert len(diagnostics) == 1
    failure = diagnostics[0].exc_info
    assert failure is not None and isinstance(failure[1], ValidationError)
    assert failure[1].errors()[0]["input"] == value
    healthy = generation_case.client.post(
        BASE + "/validate-definition", json=_definition()
    )
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["data"] == {
        "workspace_id": WORKSPACE_ID,
        "provider_available": True,
        "reason": None,
    }


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("value", [*(native * 40 for native in NATIVE_PATHS), " "])
def test_persistent_task_input_rejection_precedes_writes(
    generation_case: GenerationCase,
    method: str,
    value: str,
) -> None:
    definition = _definition()
    field = "name" if value.isspace() else "session_title"
    if field == "name":
        definition["name"] = value
    else:
        definition["config"] = {"prompt": "继续执行", "session_title": value}
    payload: dict[str, object] = {"configuration": _task_configuration(definition)}
    payload.update(
        {"idempotency_key": "create"} if method == "POST" else {"expected_revision": 1}
    )
    response = generation_case.client.request(
        method,
        "/api/v1/persistent-tasks" + ("" if method == "POST" else "/task"),
        json=payload,
    )
    assert response.status_code == 400, response.text
    assert f"{field}:string_too_" in response.json()["detail"]
    assert "gen-secret" not in response.text
    assert (
        response.json()["request_id"] == response.headers["X-Request-ID"] == REQUEST_ID
    )
    generation_case.runtime.store.create.assert_not_called()
    generation_case.runtime.store.update.assert_not_called()
    generation_case.runtime.notify.assert_not_called()


@pytest.mark.parametrize(
    ("suffix", "method", "payload"),
    [
        ("/validate-definition", "validate_definition", _definition()),
        ("/preview-placement", "preview", PREVIEW),
    ],
)
@pytest.mark.parametrize(
    ("failure", "status"),
    [(ValueError("生成参数不可用"), 400), (KeyError("生成参数不可用"), 404)],
)
def test_business_rejection_keeps_status(
    generation_case: GenerationCase,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    method: str,
    payload: dict[str, object],
    failure: Exception,
    status: int,
) -> None:
    async def reject(request: object) -> NoReturn:
        raise failure

    monkeypatch.setattr(generation_case.generation, method, reject)
    response = generation_case.client.post(BASE + suffix, json=payload)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": "生成参数不可用", "request_id": REQUEST_ID}
    assert response.headers["X-Request-ID"] == REQUEST_ID


def test_diagnostic_sink_failure_does_not_change_naming_rejection(
    generation_case: GenerationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ValidationError] = []
    sink_error = OSError(5, "原始日志故障", "/private/gen-secret/service.log")

    def fail_log(*args: object, **kwargs: object) -> NoReturn:
        original = kwargs["exc_info"]
        assert isinstance(original, ValidationError)
        captured.append(original)
        raise sink_error

    monkeypatch.setattr(failures.logger, "error", fail_log)
    definition = _definition()
    definition["config"] = {"prompt": "继续执行", "session_title": NATIVE_PATHS[0] * 40}
    response = generation_case.client.post(
        BASE + "/validate-definition", json=definition
    )
    assert response.status_code == 400, response.text
    assert "diagnostic_sink_error=OSError" in response.json()["detail"]
    assert "gen-secret" not in response.text
    assert len(captured) == 1 and "gen-secret" in "\n".join(captured[0].__notes__)
