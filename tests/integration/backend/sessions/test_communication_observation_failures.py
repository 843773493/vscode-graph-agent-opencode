"""观察失败经真实准入与冷重放保留事实，不把原始诊断写入业务记录。"""

from __future__ import annotations

import errno
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.api import messages
from app.api.deps import get_session_communication_service, verify_local_token
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.trace_middleware import GATEWAY_ID_HEADER, TraceMiddleware
from app.schemas.internal_v2.communication import CommunicationObservationRequest
from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.orchestration.communication.observation_persistence import (
    _observation_page_identity,
)
from tests.integration.backend.sessions.test_communication_observation_lifecycle import (
    GATEWAY,
    _Case,
)
from tests.integration.backend.sessions.test_communication_observation_lifecycle import (
    lifecycle_case as lifecycle_case,  # noqa: PLC0414 — 复用真实catalog/Session/control owner
)


@pytest.fixture
async def observation_client(lifecycle_case: _Case):
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(messages.router, prefix="/api/v1")
    app.dependency_overrides[verify_local_token] = lambda: "authorized-test-local"
    app.dependency_overrides[get_session_communication_service] = lambda: lifecycle_case.service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://observation-owner.test", headers={
            GATEWAY_ID_HEADER: GATEWAY, "X-BoxTeam-Communication-Origin": GATEWAY,
            "X-Request-ID": "observation-owner-failure",
        },
    ) as client:
        yield client


@pytest.fixture(params=["runtime", "value", "key", "os", "sqlite"])
def owner_failure(request: pytest.FixtureRequest, native_path: str) -> Exception:
    if request.param == "os":
        return OSError(errno.EACCES, "原生读取失败", native_path)
    if request.param == "sqlite":
        failure = sqlite3.OperationalError("原生数据库失败: " + native_path)
        failure.sqlite_errorcode = sqlite3.SQLITE_IOERR
        return failure
    return {"runtime": RuntimeError, "value": ValueError, "key": KeyError}[request.param](
        "原生投影失败: " + native_path,
    )


@pytest.fixture(params=[
    "/private/observation/workspace", r"C:\private\observation\workspace",
    r"\\server\private\observation\workspace",
])
def native_path(request: pytest.FixtureRequest) -> str:
    return request.param


async def test_owner_failure_http_durable_reopen_and_new_read_recovery(
    lifecycle_case: _Case, observation_client: httpx.AsyncClient,
    owner_failure: Exception, native_path: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = lifecycle_case
    source = GlobalThreadAddress(GATEWAY, case.workspace.workspace_id, case.source, case.source_thread)
    target = GlobalThreadAddress(GATEWAY, case.workspace.workspace_id, case.target, case.target_thread)
    call = "observation-failure-call"
    request = CommunicationObservationRequest(
        observation_id="obs_owner_failure", source_call_id=call,
        page_identity=_observation_page_identity(source, call), page_ordinal=0,
        expires_at=case.clock.now + timedelta(days=1), source=source, target=target,
        operation="read", query={"read": case.read_request().model_dump(mode="json")},
    )
    calls = 0

    async def fail_read(_request):
        nonlocal calls
        calls += 1
        raise owner_failure

    monkeypatch.setattr(case.service.observations._query, "freeze_read_context", fail_read)
    path = f"/api/v1/sessions/{case.target}/communication-observations"
    response = await observation_client.post(path, json=request.model_dump(mode="json"))
    expected_status = 500 if isinstance(owner_failure, (OSError, sqlite3.Error)) else 409
    assert response.status_code == expected_status, response.text
    assert "private" not in response.text and native_path not in response.text
    assert response.headers["X-Request-ID"] == "observation-owner-failure"
    detail = (
        f"冷观察操作失败: session_id={case.target}, thread_id={case.target_thread}, "
        f"operation=read, stage=target_observation, error_type={type(owner_failure).__name__}"
    )
    if expected_status == 500:
        assert response.json() == {
            "code": 500, "message": detail, "data": None,
            "request_id": "observation-owner-failure",
        }
    else:
        assert response.json() == {"detail": detail}
    node = case.workspace.resolver.resolve_session_node(case.target)
    with closing(SessionControlStore(node / CONTROL_DATABASE_NAME, read_only=True)) as store:
        frozen = store.get_remote_observation(request.observation_id, side="target")
        page = store.get_remote_page(request.observation_id, side="target", page_identity=request.page_identity)
        assert frozen.result == page.result
        assert page.result["error"]["code"] == "read-snapshot-mismatch"
        facts = {"type": type(owner_failure).__name__}
        for name in ("errno", "winerror", "sqlite_errorcode"):
            value = getattr(owner_failure, name, None)
            if isinstance(value, int):
                facts[name] = value
        assert page.result["owner_failure"] == facts
        assert "private" not in json.dumps(asdict(frozen))
        leases = [store.find_lease_by_operation(identity) for identity in (
            "target-observation|" + request.observation_id,
            "target-page|" + request.page_identity,
        )]
        assert all(lease.state == "failed" for lease in leases)
    assert calls == 1
    assert any(record.exc_info and record.exc_info[1] is owner_failure for record in caplog.records)
    case.restore()
    original_read = case.service.observations._query.freeze_read_context
    monkeypatch.setattr(case.service.observations._query, "freeze_read_context", fail_read)
    replay = await observation_client.post(path, json=request.model_dump(mode="json"))
    assert replay.status_code == 409 and replay.json()["detail"]["code"] == "read-snapshot-mismatch"
    assert "private" not in replay.text and calls == 1
    with closing(SessionControlStore(node / CONTROL_DATABASE_NAME, read_only=True)) as store:
        assert store.get_remote_observation(request.observation_id, side="target") == frozen
        assert store.get_remote_page(request.observation_id, side="target", page_identity=request.page_identity) == page
        assert [store.get_lease(lease.lease_id) for lease in leases] == leases
    monkeypatch.setattr(case.service.observations._query, "freeze_read_context", original_read)
    new_call = call + "-repaired"
    fresh = request.model_copy(update={
        "observation_id": "obs_owner_repaired", "source_call_id": new_call,
        "page_identity": _observation_page_identity(source, new_call),
    })
    recovered = await observation_client.post(path, json=fresh.model_dump(mode="json"))
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["data"]["result"]["items"][0]["text"] == "第一条"
    assert calls == 1
