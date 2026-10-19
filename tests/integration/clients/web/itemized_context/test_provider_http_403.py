"""固定 Provider HTTP 403 经真实 Gateway 与 Workspace 后端的 Integration。"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process

PROVIDER_API_KEY = "provider-403-integration-key"
PROVIDER_ERROR_MARKER = "provider-403-fixed-denial"


def _require_free_port(port: int) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


@dataclass
class Provider403State:
    requests: list[dict[str, object]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)


@pytest.fixture
def provider_403_ports(request: pytest.FixtureRequest) -> tuple[int, int, int]:
    port_block = integration_port_block_for_file(Path(request.node.path))
    return port_block.port(0), port_block.port(10), port_block.port(20)


@pytest.fixture
def provider_403_server(
    request: pytest.FixtureRequest,
    provider_403_ports: tuple[int, int, int],
):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    provider_port = provider_403_ports[1]
    for port in provider_403_ports:
        _require_free_port(port)
    state = Provider403State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw.decode("utf-8"))
            response_body = json.dumps(
                {
                    "error": {
                        "message": PROVIDER_ERROR_MARKER,
                        "type": "permission_error",
                        "param": None,
                        "code": "provider_forbidden",
                    }
                }
            ).encode("utf-8")
            with state.lock:
                state.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": payload,
                        "response_status": 403,
                    }
                )
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

    server = ThreadingHTTPServer(("127.0.0.1", provider_port), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield context, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        safe_requests = [
            {
                "path": row["path"],
                "authorization_matches_fixture": row["authorization"]
                == f"Bearer {PROVIDER_API_KEY}",
                "model": row["body"].get("model"),
                "stream": row["body"].get("stream"),
                "response_status": row["response_status"],
            }
            for row in state.requests
        ]
        (context.artifacts_dir / "provider-http-403-requests.json").write_text(
            json.dumps(safe_requests, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


@pytest.fixture
def provider_403_runtime(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    setup_test_config: None,
    provider_403_server,
    provider_403_ports: tuple[int, int, int],
):
    del setup_test_config
    context, _state = provider_403_server
    backend_port, provider_port, gateway_port = provider_403_ports
    workspace = Path(integration_workspace_root_path).resolve()
    assert workspace == context.workspace_root
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    endpoint = f"http://127.0.0.1:{provider_port}/v1"
    primary = next(
        provider
        for provider in config["llm"]["providers"]
        if provider["id"] == "primary"
    )
    primary.update(
        {
            "endpoint": endpoint,
            "model": "provider-403-integration-model",
            "api_key": PROVIDER_API_KEY,
            "custom_llm_provider": "openai",
        }
    )
    for provider in config["llm"]["providers"]:
        if provider["id"] == "primary":
            continue
        provider.update(
            {
                "endpoint": endpoint,
                "model": f"provider-403-unexpected-{provider['id']}",
                "api_key": f"provider-403-unexpected-{provider['id']}-key",
                "custom_llm_provider": "openai",
                "api_mode": dict(primary["api_mode"]),
            }
        )
        provider.pop("auth", None)
        provider.pop("request_options", None)
    for agent in config["agents"].values():
        agent["model"]["primary_provider"] = "primary"
        agent["model"]["fallback_providers"] = []
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    runtime_home = context.boxteam_home_for_node(request.node.nodeid)
    backend = start_backend_process(
        workspace_root=str(workspace),
        port=backend_port,
        log_name="provider-http-403-backend",
        env_overrides={
            "BOXTEAM_HOME": str(runtime_home),
            "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
        },
    )
    try:
        gateway = start_gateway_process(
            workspace_root=workspace,
            default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=gateway_port,
            boxteam_home=runtime_home,
            artifacts_dir=context.artifacts_dir,
        )
        try:
            (context.artifacts_dir / "process-owner.json").write_text(
                json.dumps(
                    {
                        "classification": "Integration",
                        "workspace": str(workspace),
                        "boxteam_home": str(runtime_home),
                        "backend_pid": backend.process.pid,
                        "backend_port": backend_port,
                        "provider_stub_port": provider_port,
                        "gateway_pid": gateway.process.pid,
                        "gateway_port": gateway_port,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            yield context, workspace, f"http://127.0.0.1:{gateway_port}"
        finally:
            close_gateway_process(gateway)
    finally:
        close_backend_process(backend)


def _assert_request_envelope(
    response: httpx.Response,
    *,
    expected_request_id: str | None = None,
) -> dict[str, object]:
    assert response.status_code == 200, response.text
    envelope = response.json()
    request_id = envelope.get("request_id")
    assert isinstance(request_id, str) and request_id
    assert response.headers.get("X-Request-ID") == request_id
    if expected_request_id is not None:
        assert request_id == expected_request_id
    return envelope


async def _wait_for_failed_job(
    client: httpx.AsyncClient,
    job_id: str,
    workspace_headers: dict[str, str],
) -> dict[str, object]:
    for _ in range(120):
        response = await client.get(
            f"/api/v1/jobs/{job_id}", headers=workspace_headers
        )
        envelope = _assert_request_envelope(response)
        job = envelope["data"]
        assert isinstance(job, dict)
        if job["status"] == "failed":
            return job
        if job["status"] in {"completed", "succeeded", "cancelled", "timed_out"}:
            pytest.fail(f"Provider 403 应终止为 failed，实际 Job={job!r}")
        await asyncio.sleep(0.25)
    pytest.fail(f"等待 Provider 403 终态超时: {job_id}")


async def _read_failed_sse(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    turn_id: str,
    thread_id: str,
    workspace_headers: dict[str, str],
) -> tuple[str, str, dict[str, object]]:
    # 与 Web streamSessionMessageEvents 所用的 Gateway 消息流路径保持一致。
    async with client.stream(
        "GET",
        f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream",
        params={"thread_id": thread_id},
        headers={**workspace_headers, "Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/event-stream")
        assert response.headers.get("X-Request-ID")
        assert response.headers.get("X-Message-Stream-ID")
        event_type: str | None = None
        event_seq: str | None = None
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event_type = line.removeprefix("event: ")
            elif line.startswith("id: "):
                event_seq = line.removeprefix("id: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
            elif not line:
                frame_type = event_type
                frame_seq = event_seq
                frame_data = "\n".join(data_lines)
                event_type = None
                event_seq = None
                data_lines = []
                if frame_type == "stream.failed":
                    assert frame_seq is not None
                    return frame_type, frame_seq, json.loads(frame_data)
        pytest.fail("消息 SSE 在返回 stream.failed 前结束")


@pytest.mark.asyncio
async def test_real_gateway_provider_http_403_is_visible_and_has_no_fallback(
    provider_403_runtime,
    provider_403_server,
):
    context, workspace, base_url = provider_403_runtime
    _, state = provider_403_server
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=LOCAL_TOKEN_HEADERS,
        timeout=30,
    ) as client:
        health = await client.get("/api/gateway/health")
        assert health.status_code == 200, health.text
        guest = await client.post(
            "/api/gateway/users/guest",
            json={"tracking": {"source": "provider-http-403-integration"}},
        )
        _assert_request_envelope(guest)
        workspaces = _assert_request_envelope(
            await client.get("/api/gateway/workspaces")
        )
        workspace_id = workspaces["data"]["active_workspace_id"]
        workspace_headers = {"X-BoxTeam-Workspace-Id": workspace_id}
        workspace_response = _assert_request_envelope(
            await client.get("/api/v1/workspace", headers=workspace_headers)
        )
        active = next(item for item in workspaces["data"]["items"] if item["workspace_id"] == workspace_id)
        assert workspace_response["data"]["workspace_id"] == active["backend_workspace_id"]
        assert "root_path" not in workspace_response["data"]
        assert "root" not in workspace_response["data"]["git"]

        create_request_id = str(uuid4())
        created = _assert_request_envelope(
            await client.post(
                "/api/v1/sessions",
                headers={**workspace_headers, "X-Request-ID": create_request_id},
                json={"title": "Provider HTTP 403 Integration"},
            ),
            expected_request_id=create_request_id,
        )
        session_id = created["data"]["session_id"]

        send_request_id = str(uuid4())
        sent = _assert_request_envelope(
            await client.post(
                f"/api/v1/sessions/{session_id}/messages",
                headers={**workspace_headers, "X-Request-ID": send_request_id},
                json={
                    "message": {"content": "PROVIDER_403_REQUEST_MARKER"},
                    "run": {
                        "mode": "single_agent",
                        "agent_id": "default",
                        "response_mode": "stream",
                        "async": True,
                        "max_steps": 1,
                        "timeout_seconds": 60,
                    },
                },
            ),
            expected_request_id=send_request_id,
        )
        accepted = sent["data"]
        job_id = accepted["job_id"]
        turn_id = accepted["turn_id"]
        thread_id = accepted["thread_id"]
        assert all(isinstance(value, str) and value for value in (job_id, turn_id, thread_id))

        job = await _wait_for_failed_job(client, job_id, workspace_headers)
        assert PROVIDER_ERROR_MARKER in job["error_message"]

        job_events = _assert_request_envelope(
            await client.get(
                f"/api/v1/jobs/{job_id}/events", headers=workspace_headers
            )
        )["data"]
        model_failures = [event for event in job_events if event["type"] == "model_failed"]
        assert len(model_failures) == 1
        assert model_failures[0]["payload"]["provider_id"] == "primary"
        assert PROVIDER_ERROR_MARKER in model_failures[0]["payload"]["error"]
        failed_jobs = [event for event in job_events if event["type"] == "job_failed"]
        assert len(failed_jobs) == 1

        stream_type, stream_event_seq, stream_failure = await asyncio.wait_for(
            _read_failed_sse(
                client,
                session_id=session_id,
                turn_id=turn_id,
                thread_id=thread_id,
                workspace_headers=workspace_headers,
            ),
            timeout=20,
        )
        assert stream_type == "stream.failed"
        assert stream_failure["type"] == stream_type
        assert isinstance(stream_failure["event_id"], str) and stream_failure["event_id"]
        assert stream_event_seq == str(stream_failure["event_seq"])
        assert PROVIDER_ERROR_MARKER in stream_failure["payload"]["message"]

        snapshot = _assert_request_envelope(
            await client.get(
                f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream/snapshot",
                params={"thread_id": thread_id},
                headers=workspace_headers,
            )
        )["data"]
        assert snapshot["stream_status"] == "failed"
        assert PROVIDER_ERROR_MARKER in snapshot["failure"]["message"]

    with state.lock:
        requests = list(state.requests)
    assert requests
    for provider_request in requests:
        assert provider_request["path"] == "/v1/chat/completions"
        assert provider_request["authorization"] == f"Bearer {PROVIDER_API_KEY}"
        assert provider_request["response_status"] == 403
        assert provider_request["body"]["model"] == "provider-403-integration-model"
        assert provider_request["body"]["stream"] is True
        assert "PROVIDER_403_REQUEST_MARKER" in json.dumps(
            provider_request["body"], ensure_ascii=False
        )
    provider_request = requests[0]

    config = commentjson.loads(
        (workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8")
    )
    assert all(
        agent["model"]["fallback_providers"] == []
        for agent in config["agents"].values()
    )
    assert [provider["id"] for provider in config["llm"]["providers"]] == [
        "primary",
        "backup_1",
        "backup_2",
        "backup_3",
        "backup_4",
    ]
    (context.artifacts_dir / "provider-http-403-result.json").write_text(
        json.dumps(
            {
                "classification": "Integration",
                "gateway_api_request_id": send_request_id,
                "job_status": job["status"],
                "job_error": job["error_message"],
                "model_failed_events": len(model_failures),
                "job_failed_events": len(failed_jobs),
                "message_sse_terminal_event": stream_type,
                "message_sse_event_id": stream_failure["event_id"],
                "message_sse_event_seq": stream_failure["event_seq"],
                "message_stream_status": snapshot["stream_status"],
                "provider_request_count": len(requests),
                "provider_http_status": provider_request["response_status"],
                "fallback_providers": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
