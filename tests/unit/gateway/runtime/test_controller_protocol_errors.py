"""生命周期响应共用解析保留错误归属，原始正文只进诊断。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController


@dataclass
class RuntimeReply:
    success: dict[str, object]
    payload: object
    status: int = 200
    content: bytes | None = None
    requests: list[httpx.Request] = field(default_factory=list)


@pytest.fixture(params=["status", "action", "pending-status", "pending-retry", "pending-contract"])
def entry(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def runtime_reply(entry: str) -> RuntimeReply:
    if entry == "pending-contract":
        data = {"candidate_ref": "candidate_test", "candidate_id": "candidate_id",
                "pending_revision": 1, "candidate_digest": "candidate_digest",
                "effective_digest": "effective_digest", "target_generation": "generation_test",
                "fencing_token": "private-fence-token", "secret_binding_digest": "binding_digest"}
    elif entry.startswith("pending-"):
        data = {"restart_required": False}
    else:
        data = {"lifecycle_state": "ready", "accepting_jobs": True, "blockers": []}
    return RuntimeReply(data, {"data": data})


@pytest.fixture
def invoke_runtime(
    entry: str, runtime_reply: RuntimeReply, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Awaitable[object]]:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway"))
    controller = GatewayWorkspaceRuntimeController(
        registry=GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json"),
        project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    target = WorkspaceTarget(
        workspace_id="workspace_protocol", name="协议测试", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        backend_workspace_id="00000000-0000-4000-8000-000000000001",
    )
    client_class = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        runtime_reply.requests.append(request)
        if entry in {"pending-retry", "pending-contract"} and request.url.path.endswith("/reload-status"):
            return httpx.Response(200, json={"data": {
                "state": "recovery_required" if entry == "pending-retry" else "pending",
                "restart_required": True, "candidate_ref": "candidate_test",
            }})
        if runtime_reply.content is not None:
            return httpx.Response(runtime_reply.status, content=runtime_reply.content)
        return httpx.Response(runtime_reply.status, json=runtime_reply.payload)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        **options, transport=httpx.MockTransport(respond),
    ))
    if entry == "status":
        return lambda: controller._runtime_status(target, request_id="req_runtime_protocol")
    if entry == "action":
        return lambda: controller._runtime_action(target, "/api/v1/runtime/drain", request_id="req_runtime_protocol")
    return lambda: controller._pending_startup_contract(target, request_id="req_runtime_protocol")


@pytest.mark.parametrize("shape", ["root-string", "root-list", "missing", "null", "data-string", "data-list"])
async def test_protocol_error_retains_type_full_diagnostics_and_retry(
    invoke_runtime: Callable[[], Awaitable[object]], runtime_reply: RuntimeReply,
    entry: str, shape: str, caplog: pytest.LogCaptureFixture,
) -> None:
    body = "/private/runtime-marker\nC:\\private\\runtime-marker\n\\\\host\\share\\runtime-marker" + "x" * 2048
    runtime_reply.payload = {
        "root-string": body, "root-list": [body], "missing": {"detail": body},
        "null": {"data": None, "detail": body}, "data-string": {"data": body},
        "data-list": {"data": [body]},
    }[shape]
    with pytest.raises(TypeError) as caught:
        await invoke_runtime()
    assert type(caught.value) is TypeError
    assert str(caught.value) == "Workspace API 生命周期响应缺少 data 对象: status=200"
    first_requests = list(runtime_reply.requests)
    assert len(first_requests) == (2 if entry in {"pending-retry", "pending-contract"} else 1)
    for request in first_requests:
        assert request.headers["X-Request-ID"] == "req_runtime_protocol"
        assert request.headers["X-BoxTeam-Workspace-Id"] == "00000000-0000-4000-8000-000000000001"
        assert request.headers["X-BoxTeam-Gateway-Id"].startswith("gateway_")
    record, = [record for record in caplog.records if record.name == "app.gateway.runtime.controller"]
    assert json.loads(record.args[-1]) == runtime_reply.payload
    assert "private-fence-token" not in caplog.text
    runtime_reply.payload = {"data": runtime_reply.success}
    result = await invoke_runtime()
    expected = None if entry in {"pending-status", "pending-retry"} else runtime_reply.success
    assert result == expected
    second_requests = runtime_reply.requests[len(first_requests):]
    assert [(request.method, request.url, request.headers, request.content) for request in first_requests] == [
        (request.method, request.url, request.headers, request.content) for request in second_requests
    ]


async def test_http_rejection_keeps_structured_blockers(
    invoke_runtime: Callable[[], Awaitable[object]], runtime_reply: RuntimeReply,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_reply.status = 409
    runtime_reply.payload = {"detail": {"status": "blocked", "blockers": [{"reason": "运行中"}]}}
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await invoke_runtime()
    assert caught.value.response.status_code == 409
    assert caught.value.response.json() == runtime_reply.payload
    assert not caplog.records


async def test_invalid_json_keeps_decoder_contract(
    invoke_runtime: Callable[[], Awaitable[object]], runtime_reply: RuntimeReply,
) -> None:
    runtime_reply.content = b'{"invalid":'
    with pytest.raises(json.JSONDecodeError) as caught:
        await invoke_runtime()
    assert caught.value.doc == runtime_reply.content.decode()
