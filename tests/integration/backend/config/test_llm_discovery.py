"""真实上游 HTTP、配置来源及探测 API；不代替 Workbench／Gateway 验收。"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.api import config as config_api
from app.api.deps import get_config_service, verify_local_token
from app.core.path_utils import workspace_context
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.gateway.secrets import ResolvedSecret
from app.schemas.internal_v2.llm_probe import LlmDiscoverRequest
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from app.testing.model_stream import (
    chat_completion_frames,
    get_protocol_codec,
    responses_frames,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

GATEWAY = "gateway_" + "a" * 32
SOURCE = "workspace_root_mutable_override"
URL = "/api/v1/config/llm/discover"
SECRET_ID = "sec_" + "b" * 32


@dataclass
class Catalog:
    endpoint: str = ""
    pages: list[object] = field(default_factory=lambda: [{"data": [{"id": "upstream-main"}]}])
    directory_pages: dict[str, object] = field(default_factory=dict)
    requests: list[tuple[str, httpx.Headers]] = field(default_factory=list)
    wait_for_cancel: bool = False
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    model_requests: list[dict] = field(default_factory=list)
    text: str = "Hello from the configured model."
    deltas: list[str] | None = None
    status: int = 200
    truncate: bool = False
    connections: set[asyncio.StreamWriter] = field(default_factory=set, repr=False)
    pause_before_terminal: bool = False
    content_sent: asyncio.Event = field(default_factory=asyncio.Event)
    release_terminal: asyncio.Event = field(default_factory=asyncio.Event)
    blocked_models: set[str] = field(default_factory=set)
    delayed_models: dict[str, float] = field(default_factory=dict)
    model_events_path: Path | None = None


@pytest.fixture
async def catalog() -> AsyncIterator[Catalog]:
    catalog = Catalog()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        catalog.connections.add(writer)
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            path = head[0].split(" ")[1]
            headers = httpx.Headers((name, value.strip()) for name, value in
                (line.split(":", 1) for line in head[1:] if line))
            catalog.requests.append((path, headers))
            body = json.loads(await reader.readexactly(int(headers["content-length"]))) if "content-length" in headers else None
            if body is not None:
                catalog.model_requests.append(body)
            catalog.opened.set()
            if catalog.wait_for_cancel or body is not None and body["model"] in catalog.blocked_models:
                def record(event: str) -> None:
                    if catalog.model_events_path is not None:
                        with catalog.model_events_path.open("a", encoding="utf-8") as output:
                            output.write(json.dumps({"event": event, "model": body["model"]}) + "\n")

                record("opened")
                assert await reader.read() == b""
                catalog.disconnected.set()
                record("disconnected")
                return
            if body is not None:
                model = body["model"]
                protocol = {"/v1/chat/completions": "chat_completions", "/v1/responses": "responses",
                            "/v1/messages": "anthropic_messages"}[path]
                if model in catalog.delayed_models:
                    await asyncio.sleep(catalog.delayed_models[model])
                if catalog.status != 200:
                    page = {"error": {"message": "selected model rejected", "type": "invalid_request_error", "code": "rejected"}}
                elif protocol == "responses":
                    frames = responses_frames(response_id="resp-probe", model=model, output=[{
                        "type": "message", "id": "msg-probe", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": catalog.text, "annotations": []}],
                    }], usage={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8})
                    page = frames[-1].payload["response"]
                elif protocol == "anthropic_messages":
                    page = {"id": "msg-probe", "type": "message", "model": model, "role": "assistant",
                            "content": [{"type": "text", "text": catalog.text}], "stop_reason": "end_turn",
                            "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 5}}
                else:
                    page = {"id": "chat-probe", "object": "chat.completion", "created": 1, "model": model,
                            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": catalog.text}}],
                            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}
                if body.get("stream") and catalog.status == 200:
                    if protocol == "chat_completions":
                        frames = chat_completion_frames(response_id="chat-probe", model=model,
                            deltas=[{"role": "assistant", "content": text} for text in (catalog.deltas or [catalog.text])], finish_reason="stop",
                            usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8})
                        codec = get_protocol_codec("openai_chat_sse")
                    else:
                        assert protocol == "responses", "本 fixture 的 Anthropic 场景只验证真实非流式"
                        codec = get_protocol_codec("openai_responses_sse")
                    body_bytes = b"".join(codec.encode(frame) for frame in (frames[:1] if catalog.truncate else frames))
                    if catalog.pause_before_terminal and not catalog.truncate:
                        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
                                     + b"".join(codec.encode(frame) for frame in frames[:-1]))
                        await writer.drain()
                        catalog.content_sent.set()
                        await catalog.release_terminal.wait()
                        writer.write(codec.encode(frames[-1]))
                        await writer.drain()
                        return
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n" + body_bytes)
                    await writer.drain()
                    return
            else:
                page = catalog.directory_pages.get(path, catalog.pages[1 if "?after_id=" in path else 0])
            body = page if isinstance(page, bytes) else json.dumps(page).encode()
            writer.write(f"HTTP/1.1 {catalog.status} Response\r\nContent-Type: application/json\r\nContent-Length: ".encode()
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            catalog.connections.discard(writer)
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    catalog.endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    async with server:
        try:
            yield catalog
        finally:
            for writer in tuple(catalog.connections):
                writer.close()
                await writer.wait_closed()


@dataclass
class ProbeCase:
    service: ConfigService
    store: WorkspaceStateStore
    client: httpx.AsyncClient
    payload: dict
    source: Path
    user_source: Path

    def draft(self, *, provider_id: str = "team") -> dict:
        document = self.service.get_source_document(source_key=SOURCE, gateway_id=GATEWAY)
        return {
            "gateway_id": GATEWAY, "workspace_id": document.workspace_id,
            "mode": "draft", "provider_id": provider_id, "edit_version": 3,
            "source_key": SOURCE, "jsonc": document.jsonc,
            "expected_source_baseline": {key: value.model_dump() for key, value in document.source_baseline.items()},
        }

    def model_test(self, *, protocol: str = "chat_completions", **changes) -> dict:
        request = self.draft()
        payload = json.loads(request["jsonc"])
        defaults = payload["llm"]["providers"]["team"]["defaults"]
        defaults["api_mode"]["protocol"] = protocol
        defaults["custom_llm_provider"] = "anthropic" if protocol == "anthropic_messages" else "openai"
        if protocol == "anthropic_messages":
            defaults["endpoint"] = defaults["endpoint"].removesuffix("/v1")
        return {**request, "jsonc": json.dumps(payload), "model_id": "main", "credential_input": {
            "scope": "provider", "provider_id": "team", "value": "fixture-model-key",
        }, **changes}

    def rows(self) -> tuple[str, ...]:
        with closing(self.store.connection()) as connection:
            return tuple(connection.iterdump())


@pytest.fixture
async def probe_case(request: pytest.FixtureRequest, catalog: Catalog, tmp_path: Path) -> AsyncIterator[ProbeCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_default_test_workspace(workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace")
    workspace_id = load_or_create_workspace_id(workspace)
    payload = {"config_version": 3, "llm": {"providers": {"team": {
        "defaults": {"endpoint": catalog.endpoint, "api_key": "", "custom_llm_provider": "openai",
                     "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}},
                     "request_options": {"no_proxy": True}},
        "models": {"main": {"model": "configured-main"}},
    }}}}
    source = workspace / ".boxteam/workspace.jsonc"
    source.write_text(json.dumps(payload), encoding="utf-8")
    user_source = tmp_path / "workspace.jsonc"
    user_source.write_text("{}\n", encoding="utf-8")
    store = WorkspaceStateStore(workspace_root=workspace)
    with workspace_context(workspace_id, workspace):
        service = ConfigService(config_dir=Path.cwd() / "configs", config_path=user_source,
            workspace_root=workspace, workspace_state_store=store)
        service.validate_workspace_config()
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.include_router(config_api.router, prefix="/api/v1")
        app.dependency_overrides[get_config_service] = lambda: service
        app.dependency_overrides[verify_local_token] = lambda: "test-token"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.001)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                headers={"X-BoxTeam-Gateway-Id": GATEWAY, "X-Request-ID": "catalog-request"}) as client:
                yield ProbeCase(service, store, client, payload, source, user_source)
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 3)
            listener.close()
            await service.close()
            store.close()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
async def test_new_provider_without_models_uses_real_directory_and_never_saves(probe_case: ProbeCase, catalog: Catalog, protocol: str) -> None:
    request = probe_case.draft(provider_id="new-provider")
    defaults = probe_case.payload["llm"]["providers"]["team"]["defaults"].copy()
    defaults.update(api_mode={"protocol": protocol, "model_info": {}, "supports_reasoning": {}},
                    custom_llm_provider="anthropic" if protocol == "anthropic_messages" else "openai")
    request["jsonc"] = json.dumps({"llm": {"providers": {"new-provider": {"defaults": defaults}}}})
    before, original = probe_case.rows(), probe_case.source.read_bytes()
    response = await probe_case.client.post(URL, json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    result = body["data"]
    assert response.headers["X-Request-ID"] == body["request_id"] == "catalog-request"
    assert result["complete"] and result["models"] == [{"model": "upstream-main", "name": None}]
    assert result["observation"]["source_baseline"] == request["expected_source_baseline"]
    assert result["observation"]["mode"] == "draft" and result["observation"]["edit_version"] == 3
    assert catalog.requests[0][0] == "/v1/models"
    assert probe_case.source.read_bytes() == original and probe_case.rows() == before


async def test_other_saved_source_changes_reject_stale_baseline_then_uses_pending_file(probe_case: ProbeCase, catalog: Catalog) -> None:
    request = probe_case.draft()
    # 用户级来源已保存但没有 reload／active；当前来源编辑计数保持 3。
    probe_case.user_source.write_text(json.dumps({"llm": {"providers": {"team": {
        "defaults": {"request_options": {"default_headers": {"X-Pending-Source": "saved-file"}}},
    }}}}), encoding="utf-8")
    rejected = await probe_case.client.post(URL, json=request)
    assert rejected.status_code == 409 and catalog.requests == []
    request = probe_case.draft()
    before = probe_case.rows()
    response = await probe_case.client.post(URL, json=request)
    assert response.status_code == 200, response.text
    assert catalog.requests[0][1]["X-Pending-Source"] == "saved-file"
    assert probe_case.rows() == before


async def test_temporary_key_replaces_invalid_reference_without_resolving_old_secret(probe_case: ProbeCase, catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    request = probe_case.draft()
    payload = json.loads(request["jsonc"])
    payload["llm"]["providers"]["team"]["defaults"]["api_key"] = "secret:" + SECRET_ID
    request["jsonc"] = json.dumps(payload)
    request["credential_input"] = {"scope": "provider", "provider_id": "team", "value": "temporary-test-key"}

    async def forbidden(_self, _providers):
        pytest.fail("被临时输入替换的旧引用不能被读取")

    monkeypatch.setattr(ConfigService, "prepare_llm_credentials", forbidden)
    before = probe_case.rows()
    response = await probe_case.client.post(URL, json=request)
    assert response.status_code == 200, response.text
    assert catalog.requests[0][1]["Authorization"] == "Bearer temporary-test-key"
    assert response.json()["data"]["observation"]["credential"]["source"] == "temporary"
    assert "temporary-test-key" not in response.text and "temporary-test-key" not in "".join(probe_case.rows())
    assert probe_case.rows() == before


async def test_selected_secret_resolves_once_and_freezes_actual_revision(probe_case: ProbeCase, catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    request = probe_case.draft()
    payload = json.loads(request["jsonc"])
    payload["llm"]["providers"]["team"]["defaults"]["api_key"] = "secret:" + SECRET_ID
    request["jsonc"] = json.dumps(payload)
    calls = []

    async def resolve(_self, providers):
        calls.append(providers)
        return {SECRET_ID: ResolvedSecret(revision=7, value="vault-test-value")}

    monkeypatch.setattr(ConfigService, "prepare_llm_credentials", resolve)
    response = await probe_case.client.post(URL, json=request)
    assert response.status_code == 200, response.text
    assert len(calls) == len(calls[0]) == 1
    assert response.json()["data"]["observation"]["credential"] == {
        "source": "vault", "secret_id": SECRET_ID, "revision": 7, "scope": None,
    }
    assert catalog.requests[0][1]["Authorization"] == "Bearer vault-test-value"
    assert "vault-test-value" not in response.text


@pytest.mark.parametrize("mutation", ["gateway", "workspace", "scope", "active-mixed", "stale-active"])
async def test_invalid_target_scope_and_active_versions_send_no_upstream(probe_case: ProbeCase, catalog: Catalog, mutation: str) -> None:
    request = probe_case.draft()
    if mutation == "gateway":
        request["gateway_id"] = "gateway_" + "c" * 32
    elif mutation == "workspace":
        request["workspace_id"] = "another-workspace"
    elif mutation == "scope":
        request["credential_input"] = {"scope": "model", "provider_id": "team", "model_id": "main", "value": "test-key"}
    elif mutation == "active-mixed":
        request["mode"] = "active"
    else:
        request = {key: request[key] for key in ("gateway_id", "workspace_id", "provider_id", "edit_version")}
        request.update(mode="active", expected_active_revision=999, expected_active_digest="stale")
    response = await probe_case.client.post(URL, json=request)
    assert response.status_code in {409, 422}, response.text
    assert catalog.requests == []


@pytest.mark.parametrize("second_page,category", [(b'{"data": [', "protocol"), ({"data": [{"id": "x" * (2 * 1024 * 1024)}]}, "size_limit"),
                                                ({"data": [{"id": f"model-{index}"} for index in range(5001)]}, "count_limit")])
async def test_anthropic_pages_share_limits_and_keep_completed_pages(probe_case: ProbeCase, catalog: Catalog, second_page: object, category: str) -> None:
    request = probe_case.draft()
    payload = json.loads(request["jsonc"])
    defaults = payload["llm"]["providers"]["team"]["defaults"]
    defaults["custom_llm_provider"] = "anthropic"
    defaults["api_mode"]["protocol"] = "anthropic_messages"
    request["jsonc"] = json.dumps(payload)
    catalog.pages = [{"data": [{"id": "first"}], "has_more": True, "last_id": "first"}, second_page]
    response = await probe_case.client.post(URL, json=request)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert not data["complete"] and data["error"]["category"] == category
    assert data["models"] == [{"model": "first", "name": None}]
    assert [item[0] for item in catalog.requests] == ["/v1/models", "/v1/models?after_id=first"]


async def test_cancel_closes_real_http_wait_before_first_directory_response(probe_case: ProbeCase, catalog: Catalog) -> None:
    catalog.wait_for_cancel = True
    request = LlmDiscoverRequest.model_validate(probe_case.draft())
    task = asyncio.create_task(probe_case.service.discover_llm_models(request, gateway_id=GATEWAY))
    await asyncio.wait_for(catalog.opened.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(catalog.disconnected.wait(), 3)
    assert len(catalog.requests) == 1


@pytest.mark.parametrize("protocol,streaming", [
    ("chat_completions", True), ("chat_completions", False),
    ("responses", True), ("responses", False), ("anthropic_messages", False),
])
@pytest.mark.parametrize("api_key", ["fixture-model-key", ""])
async def test_model_probe_uses_real_adapter_and_single_request(
    probe_case: ProbeCase, catalog: Catalog, protocol: str, streaming: bool,
    monkeypatch: pytest.MonkeyPatch, api_key: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-process-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-anthropic-key")
    request = probe_case.model_test(protocol=protocol, streaming=streaming, credential_input={
        "scope": "provider", "provider_id": "team", "value": api_key,
    } if api_key else None)
    payload = json.loads(request["jsonc"])
    defaults = payload["llm"]["providers"]["team"]["defaults"]
    defaults["request_options"]["parameters"] = {"temperature": 0, "top_p": None}
    request["jsonc"] = json.dumps(payload)
    before, original = probe_case.rows(), probe_case.source.read_bytes()
    document = probe_case.service.get_source_document(source_key=SOURCE, gateway_id=GATEWAY)
    inspection = await probe_case.client.post(f"/api/v1/config/sources/{SOURCE}/preflight",
        params={"provider_id": "team", "model_id": "main"}, json={
            "gateway_id": GATEWAY, "workspace_id": document.workspace_id, "source_key": SOURCE,
            "jsonc": request["jsonc"], "idempotency_key": "inspect-adapter-url",
            "base_file_digest": document.file_digest, "base_layer_revision": document.layer_revision,
            "base_layer_digest": document.layer_digest, "expected_active_revision": document.active_revision,
            "expected_active_digest": document.active_digest,
        })
    assert inspection.status_code == 200, inspection.text
    inspected = inspection.json()["data"]["model_options"]
    assert catalog.requests == [] and probe_case.rows() == before
    response = await probe_case.client.post("/api/v1/config/llm/test", json=request)
    assert response.status_code == 200, response.text
    events = [json.loads(block.split("data: ", 1)[1]) for block in response.text.strip().split("\n\n") if "data: " in block]
    assert response.text.startswith("event: start\n") and "event: delta\n" in response.text
    start, result = events[0], events[-1]
    assert result["status"] == "passed", result
    assert result["preview"] == catalog.text == "".join(event["text"] for event in events[1:-1])
    assert result["usage"] == {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}
    assert result["first_content_ms"] <= result["elapsed_ms"]
    assert start["observation"] == result["observation"]
    assert start["observation"]["credential"]["source"] == ("temporary" if api_key else "none")
    assert start["model_ref"] == result["model_ref"] == {"provider_id": "team", "model_id": "main"}
    assert all(event["request_id"] == response.headers["X-Request-ID"] == "catalog-request" for event in events)
    assert start["streaming"] == result["streaming"] == streaming
    assert len(catalog.model_requests) == 1
    headers = catalog.requests[0][1]
    assert headers.get("Authorization") == (f"Bearer {api_key}" if api_key and protocol != "anthropic_messages" else None)
    assert headers.get("X-Api-Key") == (api_key if api_key and protocol == "anthropic_messages" else None)
    assert inspected["request_url"] == str(httpx.URL(catalog.endpoint).copy_with(path=catalog.requests[0][0]))
    wire = catalog.model_requests[0]
    assert wire["model"] == "configured-main" and wire.get("stream", False) is streaming
    assert wire["temperature"] == 0 and "top_p" not in wire
    assert "max_output_tokens" not in wire and (protocol == "anthropic_messages" or "max_tokens" not in wire)
    assert probe_case.rows() == before and probe_case.source.read_bytes() == original


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
async def test_model_probe_start_precedes_first_content_and_cancel_closes_tcp(
    probe_case: ProbeCase, catalog: Catalog, streaming: bool, protocol: str,
) -> None:
    catalog.wait_for_cancel = True
    request = probe_case.model_test(streaming=streaming, protocol=protocol)
    async with probe_case.client.stream("POST", "/api/v1/config/llm/test", json=request) as response:
        assert response.status_code == 200
        lines = response.aiter_lines()
        assert await asyncio.wait_for(anext(lines), 2) == "event: start"
        start = json.loads((await anext(lines)).removeprefix("data: "))
        assert start["streaming"] == streaming and start["request_id"] == response.headers["X-Request-ID"]
        await asyncio.wait_for(catalog.opened.wait(), 3)
    try:
        await asyncio.wait_for(catalog.disconnected.wait(), 3)
    except TimeoutError:
        stacks = [[(frame.f_code.co_name, frame.f_lineno) for frame in task.get_stack()] for task in asyncio.all_tasks()]
        raise AssertionError(f"取消没有结清上游连接，仍在等待的任务: {stacks}") from None
    assert len(catalog.model_requests) == 1


@pytest.mark.parametrize("failure", ["http", "truncated"])
async def test_model_probe_failure_after_start_is_one_failed_terminal_without_retry(
    probe_case: ProbeCase, catalog: Catalog, failure: str,
) -> None:
    catalog.status = 401 if failure == "http" else 200
    catalog.truncate = failure == "truncated"
    response = await probe_case.client.post("/api/v1/config/llm/test", json=probe_case.model_test())
    assert response.status_code == 200, response.text
    assert response.text.startswith("event: start\n") and response.text.count("event: result\n") == 1
    result = json.loads(response.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert result["status"] == "failed" and result["error"]
    if failure == "http":
        assert result["error"]["category"] == "authentication"
    assert len(catalog.model_requests) == 1


async def test_model_probe_preview_limit_keeps_real_terminal_and_never_saves_temporary_key(
    probe_case: ProbeCase, catalog: Catalog,
) -> None:
    catalog.text = "界" * 23000
    request = {**probe_case.draft(), "model_id": "main",
               "credential_input": {"scope": "model", "provider_id": "team", "model_id": "main", "value": "temporary-model-key"}}
    before = probe_case.rows()
    response = await probe_case.client.post("/api/v1/config/llm/test", json=request)
    assert response.status_code == 200
    result = json.loads(response.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert result["status"] == "passed" and result["preview_truncated"]
    assert len(result["preview"].encode("utf-8")) <= 64 * 1024
    assert "temporary-model-key" not in response.text and probe_case.rows() == before
    assert catalog.requests[0][1]["Authorization"] == "Bearer temporary-model-key"


async def test_model_probe_rejects_stale_baseline_before_sse_headers(probe_case: ProbeCase, catalog: Catalog) -> None:
    request = {**probe_case.draft(), "model_id": "main"}
    probe_case.user_source.write_text('{"llm":{"providers":{}}}', encoding="utf-8")
    response = await probe_case.client.post("/api/v1/config/llm/test", json=request)
    assert response.status_code == 409 and "event-stream" not in response.headers["Content-Type"]
    assert catalog.requests == []


async def test_model_probe_redacts_secret_split_across_real_chunks(probe_case: ProbeCase, catalog: Catalog) -> None:
    catalog.deltas = ["hello fixture-", "model-", "key world"]
    response = await probe_case.client.post("/api/v1/config/llm/test", json=probe_case.model_test())
    assert response.status_code == 200
    result = json.loads(response.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert result["status"] == "passed" and result["preview"] == "hello [redacted] world"
    assert "fixture-model-key" not in response.text


async def test_model_probe_deadline_closes_upstream_after_failed_terminal(probe_case: ProbeCase, catalog: Catalog) -> None:
    # 先经真实入口完成一次调用，排除 SDK 首次加载，不把本地初始化超时冒充发头后超时。
    ready = await probe_case.client.post("/api/v1/config/llm/test", json=probe_case.model_test())
    assert '"status":"passed"' in ready.text
    catalog.wait_for_cancel = True
    response = await probe_case.client.post("/api/v1/config/llm/test", json=probe_case.model_test(timeout_seconds=1))
    assert response.status_code == 200 and response.text.startswith("event: start\n")
    result = json.loads(response.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert result["status"] == "failed" and result["error"]["category"] == "timeout"
    await asyncio.wait_for(catalog.disconnected.wait(), 3)
    assert len(catalog.model_requests) == 2


async def test_model_probe_provider_key_cannot_override_model_credential(probe_case: ProbeCase, catalog: Catalog) -> None:
    request = probe_case.model_test()
    payload = json.loads(request["jsonc"])
    payload["llm"]["providers"]["team"]["models"]["main"]["overrides"] = {"api_key": "secret:" + SECRET_ID}
    request["jsonc"] = json.dumps(payload)
    response = await probe_case.client.post("/api/v1/config/llm/test", json=request)
    assert response.status_code == 422 and "Model overrides.api_key" in response.text
    assert catalog.requests == []


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
async def test_model_probe_retryable_http_error_never_retries(
    probe_case: ProbeCase, catalog: Catalog, protocol: str,
) -> None:
    catalog.status = 500
    response = await probe_case.client.post("/api/v1/config/llm/test", json=probe_case.model_test(protocol=protocol, streaming=False))
    assert response.status_code == 200 and response.text.count("event: result\n") == 1
    result = json.loads(response.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert result["status"] == "failed" and result["error"]["status_code"] == 500
    assert len(catalog.model_requests) == 1
