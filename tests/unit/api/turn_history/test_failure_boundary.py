"""历史接口公开失败事实，原生位置和损坏输入只留原异常诊断。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import TypeAdapter, ValidationError

from app.api import session_turns
from app.api.deps import get_session_turn_history_service, verify_local_token
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.trace_middleware import TraceMiddleware
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.business.session_turn_history import SessionTurnHistoryService
from app.services.infrastructure.turn_history import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)
from tests.support.diagnostic_failures import DiagnosticFailure
from tests.support.diagnostic_failures import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414 - 真实日志写入失败装具。
)
from tests.support.diagnostic_failures import (
    sqlite_failure as sqlite_failure,  # noqa: PLC0414 - 真实 SQLite ATTACH 拒绝。
)

SESSION = "ses_019a37c08c0070008000000000000001"
THREAD = "thr_019a37c08c0070008000000000000002"
REQUEST_ID = "history-error-boundary"
BASE = f"/api/v1/sessions/{SESSION}"


@dataclass
class HistoryCase:
    client: TestClient
    service: Mock


@pytest.fixture
def history_case() -> Iterator[HistoryCase]:
    from app.main import app as workspace_app

    service = Mock(spec=SessionTurnHistoryService)
    service.bootstrap = AsyncMock()
    service.load_history = AsyncMock()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(session_turns.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_session_turn_history_service] = lambda: service
    with TestClient(application, raise_server_exceptions=False, headers={"X-Request-ID": REQUEST_ID}) as client:
        yield HistoryCase(client, service)


def _request(case: HistoryCase, route: str) -> Response:
    if route == "bootstrap":
        return case.client.get(BASE + "/bootstrap")
    path = BASE + (f"/threads/{THREAD}" if route == "thread" else "") + "/history"
    return case.client.post(path, json={"direction": "tail", "turns": 1})


@pytest.fixture
def internal_failure(request: pytest.FixtureRequest, sqlite_failure: Exception) -> Exception:
    kind = request.param
    if kind == "sqlite":
        return sqlite_failure
    if kind == "native":
        return PermissionError(13, "原生历史读取失败", "/private/history-secret.sqlite")
    if kind == "runtime":
        return RuntimeError(r"损坏的内部历史: \\target\private\history-secret.sqlite")
    if kind == "validation":
        with pytest.raises(ValidationError) as caught:
            TypeAdapter(dict[str, int]).validate_python({r"C:\private\history-secret.sqlite": "invalid"})
        return caught.value
    assert kind == "canonical"
    with pytest.raises(ItemSchemaError) as caught:
        canonical_json_bytes({"/private/history-secret.sqlite": float("nan")})
    return caught.value


@pytest.mark.parametrize("route", ["bootstrap", "session", "thread"])
@pytest.mark.parametrize("internal_failure", ["native", "sqlite", "runtime", "validation", "canonical"], indirect=True)
def test_internal_history_failure_keeps_original_diagnostic_and_structured_500(
    history_case: HistoryCase, internal_failure: Exception, route: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = (internal_failure.args, internal_failure.__cause__)
    method = history_case.service.bootstrap if route == "bootstrap" else history_case.service.load_history
    method.side_effect = internal_failure
    response = _request(history_case, route)
    assert response.status_code == 500, response.text
    body = response.json()
    assert response.headers["X-Request-ID"] == body["request_id"] == REQUEST_ID
    detail = body["detail"]
    assert detail["code"] == "turn_projection_corrupted" and detail["session_id"] == SESSION
    assert "stage=history_read" in detail["message"]
    assert f"error={type(internal_failure).__name__}" in detail["message"]
    assert "history-secret" not in response.text and "sqlite-execution-private" not in response.text
    if isinstance(internal_failure, OSError):
        assert "errno=13" in detail["message"]
    if kind := getattr(internal_failure, "sqlite_errorcode", None):
        assert f"sqlite_errorcode={kind}" in detail["message"]
    diagnostic, = [record for record in caplog.records if record.exc_info and record.exc_info[1] is internal_failure]
    assert diagnostic.name == "app.core.failures" and str(internal_failure) in caplog.text
    assert (internal_failure.args, internal_failure.__cause__) == original


@pytest.mark.parametrize("route", ["bootstrap", "session", "thread"])
def test_history_failure_survives_real_diagnostic_write_failure(
    history_case: HistoryCase, route: str, diagnostic_sink: DiagnosticFailure,
) -> None:
    failure = PermissionError(13, "历史读取失败", "/private/history-secret.sqlite")
    diagnostic_sink.primary = failure
    method = history_case.service.bootstrap if route == "bootstrap" else history_case.service.load_history
    method.side_effect = failure
    response = _request(history_case, route)
    assert response.status_code == 500, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == REQUEST_ID
    detail = response.json()["detail"]
    assert detail["code"] == "turn_projection_corrupted" and detail["session_id"] == SESSION
    assert "error=PermissionError, errno=13, diagnostic_sink_error=NotADirectoryError" in detail["message"]
    assert "history-secret" not in response.text and "diagnostic-log-secret" not in response.text
    secondary, = diagnostic_sink.errors
    assert str(secondary) in "\n".join(failure.__notes__)
    assert diagnostic_sink.records[0].exc_info is not None
    assert diagnostic_sink.records[0].exc_info[1] is failure


@pytest.mark.parametrize("route", ["bootstrap", "session", "thread"])
@pytest.mark.parametrize(("kind", "status"), [
    ("key", 404), ("value", 400), ("cursor", 400),
    ("stale_cursor", 409), ("stale_turn", 409), ("deleting", 409),
])
def test_history_business_rejection_keeps_existing_contract(
    history_case: HistoryCase, route: str, kind: str, status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failures = {
        "key": KeyError("历史 Turn 不存在"),
        "value": ValueError("历史读取预算无效"),
        "cursor": InvalidTurnCursorError("历史游标无效"),
        "stale_cursor": StaleTurnCursorError(session_id=SESSION, cursor_epoch=1, current_epoch=2),
        "stale_turn": StaleTurnReferenceError(session_id=SESSION, turn_ids=["original-turn"]),
        "deleting": SessionDeletionPendingError(SESSION),
    }
    failure = failures[kind]
    method = history_case.service.bootstrap if route == "bootstrap" else history_case.service.load_history
    method.side_effect = failure
    response = _request(history_case, route)
    assert response.status_code == status, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == REQUEST_ID
    expected = (failure.detail.model_dump(mode="json") if isinstance(failure, (StaleTurnCursorError, StaleTurnReferenceError))
                else failure.args[0] if isinstance(failure, KeyError) else str(failure))
    assert response.json()["detail"] == expected
    assert not any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
