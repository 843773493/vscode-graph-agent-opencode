"""真实 HTTP 和 SDK 验证三种协议的同步／异步完整响应。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import UUID

import commentjson
import httpx
import pytest
import uvicorn
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from app import main
from app.agents.providers.wire_finalize import (
    chat_wire_params,
    responses_wire_params,
)
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.core.turn_execution_scope import (
    ScopeCancelledError,
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.identity.model_source import MODEL_SOURCE_EXTENSION
from app.domain.itemized.serialization import normalize_wire_request
from app.gateway.auth import get_gateway_local_token
from app.gateway.secrets.vault import SecretVault
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import build_model_from_provider
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import responses_frames
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.config.test_llm_discovery import ProbeCase
from tests.integration.backend.config.test_llm_discovery import (
    catalog as catalog,  # noqa: PLC0414
)
from tests.integration.backend.config.test_llm_discovery import (
    probe_case as probe_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    _managed_gateway_skill_source,
)
from tests.support.api_helpers import create_session, with_message_run_operation_id
from tests.support.http_server import running_http_server
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

Protocol = Literal["chat_completions", "responses", "anthropic_messages"]


@pytest.fixture
def nonstream_disconnected() -> Event:
    return Event()


@pytest.fixture
def nonstream_auth_headers() -> list[dict[str, str]]:
    # 仅在内存核验实际 HTTP 认证，不写入请求正文或测试产物。
    return []


@pytest.fixture
def nonstream_response_gate() -> tuple[Event, Event]:
    entered, release = Event(), Event()
    release.set()
    return entered, release


@pytest.fixture
def nonstream_workspace(tmp_path: Path) -> Iterator[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_id = load_or_create_workspace_id(workspace)
    with workspace_context(workspace_id, workspace):
        yield workspace_id


@pytest.fixture
def nonstream_upstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, nonstream_disconnected: Event, nonstream_workspace: str,
    nonstream_auth_headers: list[dict[str, str]], nonstream_response_gate: tuple[Event, Event],
) -> Iterator[tuple[str, list[dict[str, object]]]]:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    requests: list[dict[str, object]] = []
    tool = {"id": "call-files", "name": "read_file", "input": {"path": "README.md"}}
    text = "完整回复"
    reasoning = "先检查文件"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            nonstream_auth_headers.append({
                "authorization": self.headers.get("Authorization", ""),
                "x-api-key": self.headers.get("X-Api-Key", ""),
            })
            reply_id = f"full-{len(requests)}"
            entered, release = nonstream_response_gate
            if len(requests) == 1 and not release.is_set():
                entered.set()
                if not release.wait(20):
                    self.send_error(504, "test response gate was not released")
                    return
            if body.get("stream", False) is not False:
                self.send_error(400, "this upstream only accepts nonstream requests")
                return
            model = body["model"]
            if model == "wait-for-cancel":
                self.connection.settimeout(5)
                if self.rfile.read(1) == b"":
                    nonstream_disconnected.set()
                return
            messages = body.get("input", body.get("messages", []))
            has_result = any(isinstance(message, dict) and (
                message.get("type") == "function_call_output" or message.get("role") == "tool"
                or isinstance(message.get("content"), list) and any(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in message["content"]
                )
            ) for message in messages)
            if self.path == "/v1/responses":
                # 完整正文与流式测试复用同一资产编码，避免另造 Responses 协议实现。
                terminal = responses_frames(response_id=f"resp-{reply_id}", model=model, output=[
                    {"type": "reasoning", "id": f"rs-{reply_id}", "summary": [
                        {"type": "summary_text", "text": reasoning},
                    ], "encrypted_content": "opaque-reasoning"},
                    {"type": "message", "id": f"msg-{reply_id}", "role": "assistant", "status": "completed",
                     "content": [{"type": "output_text", "text": text, "annotations": []}]},
                    *([] if has_result else [{"type": "function_call", "id": f"fc-{reply_id}", "call_id": tool["id"],
                     "name": tool["name"], "arguments": json.dumps(tool["input"]), "status": "completed"}]),
                ], usage={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8})[-1].payload
                assert isinstance(terminal, dict)
                reply = terminal["response"]
            elif self.path == "/v1/messages":
                reply = {
                    "id": f"msg-{reply_id}", "type": "message", "model": model, "role": "assistant",
                    "content": [{"type": "thinking", "thinking": reasoning, "signature": "opaque-signature"},
                                {"type": "text", "text": text}, *([] if has_result else [{"type": "tool_use", **tool}])],
                    "stop_reason": "end_turn" if has_result else "tool_use", "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                }
            else:
                assert self.path == "/v1/chat/completions"
                reply = {
                    "id": f"chat-{reply_id}", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0, "finish_reason": "stop" if has_result else "tool_calls", "message": {
                        "role": "assistant", "content": text, "reasoning_content": reasoning,
                        "tool_calls": [] if has_result else [{"id": tool["id"], "type": "function", "function": {
                            "name": tool["name"], "arguments": json.dumps(tool["input"]),
                        }}],
                    }}], "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                }
            encoded = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", requests
        finally:
            nonstream_response_gate[1].set()
            (tmp_path / "received.json").write_text(json.dumps(requests), encoding="utf-8")


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
async def test_quick_probe_rejects_streaming_then_explicit_nonstream_uses_job_upstream(
    probe_case: ProbeCase, nonstream_upstream: tuple[str, list[dict[str, object]]], protocol: Protocol,
) -> None:
    endpoint, requests = nonstream_upstream
    request = probe_case.model_test(protocol=protocol, streaming=True)
    payload = json.loads(request["jsonc"])
    defaults = payload["profiles"]["team"]["settings"]["model_options"]
    defaults["endpoint"] = endpoint.removesuffix("/v1") if protocol == "anthropic_messages" else endpoint
    defaults["api_mode"].update({"model_info": {"supports_function_calling": True, "supports_reasoning": True},
        "supports_reasoning": {
            "chat_completions": {"reasoning_content": True},
            "responses": {"reasoning_items": {"summary": True, "encrypted_content": True}},
            "anthropic_messages": {"thinking_blocks": {"thinking": True, "redacted_thinking": True}},
        }[protocol]})
    defaults["request_options"]["parameters"] = {"max_output_tokens": 128}
    request["jsonc"] = json.dumps(payload)
    source_before, rows_before = probe_case.source.read_bytes(), probe_case.rows()
    # 首次 SDK 导入也属于产品 60 秒期限；调用方不能先以 HTTPX 默认 5 秒截断。
    rejected = await probe_case.client.post("/api/v1/config/llm/test", json=request, timeout=65)
    assert rejected.status_code == 200 and rejected.text.startswith("event: start\n"), rejected.text
    assert rejected.text.count("event: result\n") == 1
    failure = json.loads(rejected.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert failure["status"] == "failed" and failure["error"]["status_code"] == 400, failure
    assert len(requests) == 1 and requests[0]["stream"] is True, "不能自动切换模式或重试"
    accepted = await probe_case.client.post("/api/v1/config/llm/test", json={**request, "streaming": False}, timeout=65)
    assert accepted.status_code == 200 and accepted.text.startswith("event: start\n"), accepted.text
    terminal = json.loads(accepted.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert terminal["status"] == "passed" and terminal["preview"] == "完整回复", terminal
    assert terminal["usage"] == {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}
    assert len(requests) == 2 and requests[1].get("stream", False) is False
    assert probe_case.source.read_bytes() == source_before and probe_case.rows() == rows_before


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
@pytest.mark.parametrize("entry", ["invoke", "ainvoke", "stream", "astream"])
async def test_configured_nonstream_uses_real_http_and_preserves_complete_message(
    nonstream_upstream: tuple[str, list[dict[str, object]]], protocol: Protocol, entry: str,
) -> None:
    endpoint, requests = nonstream_upstream
    reasoning = {
        "chat_completions": {"reasoning_content": True},
        "responses": {"reasoning_items": {"summary": True, "encrypted_content": True}},
        "anthropic_messages": {"thinking_blocks": {"thinking": True, "redacted_thinking": True}},
    }[protocol]
    model = build_model_from_provider(provider={
        "provider_id": "local-test", "model_id": "full-response", "model": "test-model",
        "custom_llm_provider": "anthropic" if protocol == "anthropic_messages" else "openai",
        "endpoint": endpoint.removesuffix("/v1") if protocol == "anthropic_messages" else endpoint,
        "api_key": "fixture-key",
        "proxy_routes": ProxyRoutes(()),
        "api_mode": {"protocol": protocol, "streaming": False,
                     "supports_reasoning": reasoning,
                     "model_info": {"supports_reasoning": True, "supports_function_calling": True}},
        "request_options": {"parameters": {"temperature": 0.5, "top_p": None,
                                           "max_output_tokens": 128,
                                           "extra_body": {"vendor_nullable": None}}},
    }, runtime_config={"temperature": 0}, invocation_purpose="tool_test")
    model.max_retries = 0
    messages = [HumanMessage(content="检查文件")]
    run_id = UUID("00000000-0000-4000-8000-000000000001")
    config = {"run_id": run_id}
    if entry == "invoke":
        result = model.invoke(messages, config=config)
    elif entry == "ainvoke":
        result = await model.ainvoke(messages, config=config)
    else:
        chunks = list(model.stream(messages, config=config)) if entry == "stream" else [
            chunk async for chunk in model.astream(messages, config=config)
        ]
        assert len(chunks) == 1 and isinstance(chunks[0], AIMessageChunk)
        result = chunks[0]
    assert isinstance(result, AIMessage)
    assert LangChainMessageCodec().model_call_id(result) == str(run_id)
    assert result.text == "完整回复"
    assert result.tool_calls == [{"type": "tool_call", "id": "call-files", "name": "read_file", "args": {"path": "README.md"}}]
    assert result.usage_metadata is not None
    assert {key: result.usage_metadata[key] for key in ("input_tokens", "output_tokens", "total_tokens")} == {
        "input_tokens": 3, "output_tokens": 5, "total_tokens": 8,
    }
    assert "先检查文件" in json.dumps(result.content, ensure_ascii=False)
    if protocol == "responses":
        assert "opaque-reasoning" in json.dumps(result.content)
    elif protocol == "anthropic_messages":
        assert "opaque-signature" in json.dumps(result.content)
    request, = requests
    assert request["model"] == "test-model" and request.get("stream", False) is False
    assert "stream_options" not in request and "top_p" not in request
    assert request["temperature"] == 0
    # 当前 Anthropic adapter 将扩展保留在 extra_body，OpenAI adapter 在 HTTP 层展开。
    extensions = request["extra_body"] if protocol == "anthropic_messages" else request
    assert isinstance(extensions, dict)
    assert "vendor_nullable" in extensions and extensions["vendor_nullable"] is None
    if protocol != "anthropic_messages":
        params = responses_wire_params(model) if protocol == "responses" else chat_wire_params(model, stream=False)
        assert all(request[key] == value for key, value in params.items())


@pytest.mark.parametrize("entry", ["invoke", "ainvoke"])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
async def test_explicit_no_auth_does_not_use_process_credentials_over_real_http(
    nonstream_upstream: tuple[str, list[dict[str, object]]], nonstream_auth_headers: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch, entry: str, protocol: Protocol,
) -> None:
    endpoint, requests = nonstream_upstream
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-process-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "unrelated-admin-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-anthropic-key")
    reasoning = {
        "chat_completions": {"reasoning_content": True},
        "responses": {"reasoning_items": {"summary": True, "encrypted_content": True}},
        "anthropic_messages": {"thinking_blocks": {"thinking": True, "redacted_thinking": True}},
    }[protocol]
    model = build_model_from_provider(provider={
        "provider_id": "anonymous", "model_id": "local", "model": "test-model",
        "endpoint": endpoint.removesuffix("/v1") if protocol == "anthropic_messages" else endpoint,
        "api_key": "", "custom_llm_provider": "anthropic" if protocol == "anthropic_messages" else "openai",
        "proxy_routes": ProxyRoutes(()),
        "api_mode": {"protocol": protocol, "streaming": False,
                     "model_info": {"supports_reasoning": True, "supports_function_calling": True},
                     "supports_reasoning": reasoning},
    }, runtime_config={}, invocation_purpose="tool_test")
    try:
        messages = [HumanMessage(content="检查匿名模型")]
        result = model.invoke(messages) if entry == "invoke" else await model.ainvoke(messages)
        assert result.text == "完整回复"
        assert nonstream_auth_headers == [{"authorization": "", "x-api-key": ""}]
        assert len(requests) == 1
        assert requests[0]["model"] == "test-model"
        assert requests[0].get("stream", False) is False
    finally:
        await model.aclose()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
async def test_turn_cancel_closes_nonstream_http_before_first_content(
    nonstream_upstream: tuple[str, list[dict[str, object]]], nonstream_disconnected: Event, protocol: Protocol,
) -> None:
    endpoint, requests = nonstream_upstream
    model = build_model_from_provider(provider={
        "provider_id": "local-test", "model_id": "cancel-test", "model": "wait-for-cancel",
        "custom_llm_provider": "anthropic" if protocol == "anthropic_messages" else "openai",
        "endpoint": endpoint.removesuffix("/v1") if protocol == "anthropic_messages" else endpoint,
        "api_key": "fixture-key", "api_mode": {"protocol": protocol, "streaming": False,
                                                 "model_info": {"supports_reasoning": False}, "supports_reasoning": {}},
        "proxy_routes": ProxyRoutes(()),
        "request_options": {"parameters": {"max_output_tokens": 128}},
    }, runtime_config={}, invocation_purpose="tool_test")
    model.max_retries = 0
    scope = TurnExecutionScope(turn_stream_id="nonstream-cancel")
    token = set_current_turn_execution_scope(scope)
    try:
        task = asyncio.create_task(model.ainvoke([HumanMessage(content="等待取消")]))
        async with asyncio.timeout(5):
            while not requests:
                assert not task.done()
                await asyncio.sleep(0.01)
            await scope.cancel("user_cancelled")
            with pytest.raises(ScopeCancelledError, match="user_cancelled"):
                await task
            assert await asyncio.to_thread(nonstream_disconnected.wait, 2)
        assert len(requests) == 1
    finally:
        reset_current_turn_execution_scope(token)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.parametrize("protocol", ["responses", "anthropic_messages"])
async def test_canonical_history_replays_private_blocks_only_to_same_model_over_real_http(
    nonstream_upstream: tuple[str, list[dict[str, object]]], nonstream_workspace: str,
    monkeypatch: pytest.MonkeyPatch, protocol: Protocol, tmp_path: Path,
) -> None:
    endpoint, requests = nonstream_upstream
    monkeypatch.setenv("NONSTREAM_MODEL_KEY", "fixture-only-value")
    reasoning = {"reasoning_items": {"summary": True, "encrypted_content": True}} if protocol == "responses" else {
        "thinking_blocks": {"thinking": True, "redacted_thinking": True},
    }
    config = {"config_version": 4, "extends": "builtin:workspace", "settings": {"workspace": {
        "llm": {"providers": {"service": {"config_ref": "#/profiles/service", "models": {
            model_id: {"model": "test-model", "config_ref": "context:provider/service"}
            for model_id in ("main", "fallback")
        }}}}}}, "profiles": {"service": {"extends": "context:workspace", "settings": {
        "network": {"proxy": {"mode": "direct"}}, "model_options": {
            "endpoint": endpoint if protocol == "responses" else endpoint.removesuffix("/v1"),
            "custom_llm_provider": "openai" if protocol == "responses" else "anthropic",
            "api_key": "${NONSTREAM_MODEL_KEY}",
            "api_mode": {"protocol": protocol, "streaming": False, "supports_reasoning": reasoning,
                         "model_info": {"supports_reasoning": True, "supports_function_calling": True}},
            "request_options": {"parameters": {"max_output_tokens": 128}},
        },
    }}}}
    install_user_configuration(config_root=tmp_path, profile="default")
    source = tmp_path / "workspace.jsonc"
    source.write_text(json.dumps(config), encoding="utf-8")
    service = ConfigService(config_dir=tmp_path, config_path=source)
    service.validate_workspace_config()
    with service.use_llm_credentials({}):
        models = [build_model_from_provider(
            service.get_llm_model({"provider_id": "service", "model_id": model_id}), {},
            gateway_id="gateway-fixture", invocation_purpose="tool_test",
        ) for model_id in ("main", "fallback")]
    for model in models:
        model.max_retries = 0
        assert model.model_source is not None
        assert model.model_source.workspace_id == nonstream_workspace
        assert model.model_source.authentication == "env:NONSTREAM_MODEL_KEY"
        assert "fixture-only-value" not in repr(model.model_source)
    prompt = HumanMessage(content="检查文件")
    first = await models[0].ainvoke([prompt])
    assert first.response_metadata["model_source"] == models[0].model_source.to_dict()
    codec = LangChainMessageCodec()
    items = codec.items_for_message(first, item_sequence=1, message_id="model-reply", turn_id="turn-a",
                                    execution_id="execution-a", timestamp="2026-11-17T00:00:00Z")
    restored = codec.from_dict(codec.project_message(items))
    history = [prompt, restored, ToolMessage(content="文件列表", tool_call_id="call-files")]
    await models[0].ainvoke(history)
    await models[1].ainvoke(history)
    assert len(requests) == 3
    private = "opaque-reasoning" if protocol == "responses" else "opaque-signature"
    assert private in json.dumps(requests[1], ensure_ascii=False)
    assert private not in json.dumps(requests[2], ensure_ascii=False)
    assert "完整回复" in json.dumps(requests[2], ensure_ascii=False)
    assert "先检查文件" in json.dumps(requests[2], ensure_ascii=False)
    assert "call-files" in json.dumps(requests[2], ensure_ascii=False)


@pytest.fixture
async def nonstream_job_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
    nonstream_upstream: tuple[str, list[dict[str, object]]],
    unused_tcp_port_factory: Callable[[], int],
) -> AsyncIterator[tuple[httpx.AsyncClient, Path, list[dict[str, object]], TestRunContext, Protocol, str]]:
    """正式消息入口建立全部执行事实；保留真实 HTTP 验证 SDK 与封存请求的一致性。"""
    protocol: Protocol = request.param
    endpoint, requests = nonstream_upstream
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_default_test_workspace(workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace")
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    reasoning = {
        "chat_completions": {"reasoning_content": True},
        "responses": {"reasoning_items": {"summary": True, "encrypted_content": True}},
        "anthropic_messages": {"thinking_blocks": {"thinking": True, "redacted_thinking": True}},
    }[protocol]
    provider = config["settings"]["workspace"]["llm"]["providers"]["primary"]
    config["profiles"]["provider.primary"]["settings"]["network"] = {"proxy": {"mode": "direct"}}
    config["profiles"]["provider.primary"]["settings"]["model_options"] = {
        "endpoint": endpoint.removesuffix("/v1") if protocol == "anthropic_messages" else endpoint,
        "custom_llm_provider": "anthropic" if protocol == "anthropic_messages" else "openai",
        "api_key": "${NONSTREAM_JOB_KEY}",
        "api_mode": {"protocol": protocol, "streaming": False, "supports_reasoning": reasoning,
                     "model_info": {"supports_function_calling": True, "supports_reasoning": True}},
        "request_options": {"parameters": {"max_output_tokens": 128,
            "temperature": 0, "top_p": None, "extra_body": {"vendor_nullable": None}}},
    }
    provider["models"] = {model_id: {"model": "test-model", "config_ref": "context:provider/primary"}
        for model_id in ("default", "alternate")}
    for agent in config["settings"]["workspace"]["agents"].values():
        agent["model"] = {"primary": {"provider_id": "primary", "model_id": "default"},
                          "fallbacks": [{"provider_id": "primary", "model_id": "alternate"}]}
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    workspace_id = load_or_create_workspace_id(workspace)
    monkeypatch.setenv("NONSTREAM_JOB_KEY", "fixture-job-key")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    home = context.boxteam_home_for_node(request.node.nodeid)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    install_user_configuration(config_root=home / "config", profile="default", workspace_root=workspace)
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    monkeypatch.delenv("BOXTEAM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("BOXTEAM_TEST_MODEL_STREAM_CONFIG", raising=False)
    monkeypatch.setattr(main, "_model_stream_controller", None)
    monkeypatch.setenv("BOXTEAM_MOUNTED_WORKSPACES", json.dumps([{
        "workspace_id": workspace_id, "workspace_root": str(workspace),
        "config_candidate_ref": None, "config_generation": None, "config_fencing_token": None,
    }]))
    port, backend_port = unused_tcp_port_factory(), unused_tcp_port_factory()
    backend_url = f"http://127.0.0.1:{backend_port}"
    async with _managed_gateway_skill_source(workspace=workspace, workspace_id=workspace_id,
        boxteam_home=home, port=port, artifacts_dir=context.artifacts_dir,
        backend_url=backend_url) as (gateway_id, _owner):
        monkeypatch.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{port}")
        async with main.lifespan(main.app) as lifespan:
            server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=backend_port,
                log_level="error", access_log=False, log_config=None, lifespan="off"))
            serving = asyncio.create_task(server.serve())
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if serving.done():
                            await serving
                        await asyncio.sleep(0.01)
                await lifespan["after_http_startup"]()
                async with httpx.AsyncClient(base_url=backend_url,
                    headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": workspace_id,
                             "X-BoxTeam-Gateway-Id": gateway_id}, timeout=30) as client:
                    yield client, workspace, requests, context, protocol, gateway_id
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, timeout=10)


@pytest.mark.parametrize("nonstream_job_backend", ["chat_completions", "responses", "anthropic_messages"], indirect=True)
async def test_product_job_nonstream_tool_loop_and_model_source_match_sealed_wire(nonstream_job_backend) -> None:
    client, workspace, requests, context, protocol, gateway_id = nonstream_job_backend
    session = await create_session(client, "非流式正式执行与来源回放")
    jobs: list[dict[str, object]] = []
    for index in range(3):
        if index == 2:
            selected = await client.patch(f"/api/v1/sessions/{session}", json={
                "model": {"provider_id": "primary", "model_id": "alternate"},
            })
            assert selected.status_code == 200, selected.text
            assert selected.json()["data"]["current_model"] == {"provider_id": "primary", "model_id": "alternate"}
        sent = await client.post(f"/api/v1/sessions/{session}/messages", json=with_message_run_operation_id({
            "message": {"content": f"第 {index + 1} 次检查 README.md，保留工具结果和模型来源。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        }))
        assert sent.status_code == 200, sent.text
        assert sent.headers["X-Request-ID"] == sent.json()["request_id"]
        accepted = sent.json()["data"]
        async with asyncio.timeout(45):
            while True:
                status = await client.get(f"/api/v1/jobs/{accepted['job_id']}")
                assert status.status_code == 200, status.text
                job = status.json()["data"]
                if job["status"] in {"completed", "failed", "cancelled", "timed_out", "execution_lost"}:
                    break
                await asyncio.sleep(0.02)
        assert job["status"] == "completed", job
        jobs.append(job)
    assert len(requests) == 4, requests
    assert all(request.get("stream", False) is False for request in requests)
    assert all("stream_options" not in request for request in requests)
    for request in requests:
        assert request["temperature"] == 0 and "top_p" not in request
        extensions = request["extra_body"] if protocol == "anthropic_messages" else request
        assert "vendor_nullable" in extensions and extensions["vendor_nullable"] is None
    private = {"responses": "opaque-reasoning", "anthropic_messages": "opaque-signature"}.get(protocol)
    if private is not None:
        assert private in json.dumps(requests[1]) and private in json.dumps(requests[2])
        assert private not in json.dumps(requests[3])
    assert "先检查文件" in json.dumps(requests[3], ensure_ascii=False)
    assert "完整回复" in json.dumps(requests[3], ensure_ascii=False)
    thread = accepted["thread_id"]
    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    rollout = resolver.resolve_thread_node(session, thread) / "rollout"
    items = [json.loads(line) for line in (rollout / "rollout.jsonl").read_text(encoding="utf-8").splitlines()]
    results = [item for item in items if item["semantic_kind"] == "tool_result"]
    assert results and all(item["status"] == "completed" for item in results)
    sources = [item["extensions"][MODEL_SOURCE_EXTENSION]["value"] for item in items
               if MODEL_SOURCE_EXTENSION in item.get("extensions", {})]
    assert sources and {source["model_id"] for source in sources} == {"default", "alternate"}
    assert all(source["gateway_id"] == gateway_id and source["workspace_id"] == load_or_create_workspace_id(workspace)
               and source["authentication"] == "env:NONSTREAM_JOB_KEY" for source in sources)
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as cold:
        assemblies = cold.list_context_assemblies(session, thread)
        assert len(assemblies) == len(requests)
        for assembly, request in zip(assemblies, requests, strict=True):
            assert assembly.request_hash_preimage == normalize_wire_request(request)
            assert assembly.request_hash == context_request_hash(
                assembly.as_sealed_plan(), assembly.provider_version, projector_id=assembly.projector_id,
                projector_version=assembly.projector_version, target_format=assembly.target_format,
                wire_request=assembly.request_hash_preimage,
            )
    (context.artifacts_dir / "nonstream-job-wire.json").write_text(json.dumps({
        "jobs": jobs, "requests": requests, "assemblies": [assembly.to_dict() for assembly in assemblies],
        "model_sources": sources,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("nonstream_job_backend", ["chat_completions"], indirect=True)
async def test_product_jobs_freeze_credentials_and_rotate_only_new_execution(
    nonstream_job_backend, nonstream_auth_headers: list[dict[str, str]], nonstream_response_gate: tuple[Event, Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实密钥 writer、配置 CAS、Job、SDK 与 TCP；只观测 vault 读取，不替代其实现。"""
    client, workspace, requests, context, _protocol, gateway_id = nonstream_job_backend
    secret_id = "sec_00000000000040008000000000000001"
    before, after = "fixture-key-before-rotation", "fixture-key-after-rotation"
    resolved_ids: list[list[str]] = []
    resolve = SecretVault.resolve

    def observe_resolve(vault: SecretVault, ids: list[str]):
        resolved_ids.append(list(ids))
        return resolve(vault, ids)

    monkeypatch.setattr(SecretVault, "resolve", observe_resolve)

    async def submit(session_id: str) -> dict:
        response = await client.post(f"/api/v1/sessions/{session_id}/messages", json=with_message_run_operation_id({
            "message": {"content": "检查 README.md 并报告结果"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        }))
        assert response.status_code == 200, response.text
        return response.json()["data"]

    async def finish(accepted: dict, *, expected: str = "completed") -> dict:
        async with asyncio.timeout(30):
            while True:
                response = await client.get(f"/api/v1/jobs/{accepted['job_id']}")
                assert response.status_code == 200, response.text
                job = response.json()["data"]
                if job["status"] in {"completed", "failed", "cancelled", "timed_out", "execution_lost"}:
                    assert job["status"] == expected, job
                    return job
                await asyncio.sleep(0.02)

    async with httpx.AsyncClient(base_url=os.environ["BOXTEAM_GATEWAY_URL"], trust_env=False,
        headers={"X-Local-Token": get_gateway_local_token()}, timeout=15) as gateway:
        created = await gateway.post("/api/gateway/secrets", json={
            "id": secret_id, "name": "执行共享密钥", "value": before,
        })
        assert created.status_code == 200, created.text
        assert created.json()["data"]["item"]["revision"] == 1

        source_key = "workspace_root_mutable_override"
        source_url = f"/api/v1/config/sources/{source_key}"
        document_response = await client.get(source_url)
        assert document_response.status_code == 200, document_response.text
        document = document_response.json()["data"]
        source = commentjson.loads(document["jsonc"])
        primary = source["profiles"]["provider.primary"]["settings"]["model_options"]
        primary["api_key"] = "secret:" + secret_id
        # 未使用的失效引用不能阻断列表、保存或实际 Agent 的密钥准备。
        source["settings"]["workspace"]["llm"]["providers"]["unused"] = {"config_ref": "#/profiles/unused",
            "models": {"unused": {"model": "unused-model", "config_ref": "context:provider/unused"}}}
        source["profiles"]["unused"] = {"extends": "context:workspace", "settings": {"model_options": {
            **primary, "api_key": "secret:sec_00000000000040008000000000000099",
        }}}
        saved = await client.put(source_url, json={
            "gateway_id": gateway_id, "workspace_id": document["workspace_id"], "source_key": source_key,
            "jsonc": json.dumps(source, ensure_ascii=False), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": "bind-execution-shared-secret",
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["edit"]["state"] == "active", saved.text
        assert resolved_ids == []

        old_session = await create_session(client, "轮换前已在途")
        new_session = await create_session(client, "轮换后新执行")
        selected = await client.patch(f"/api/v1/sessions/{new_session}", json={
            "model": {"provider_id": "primary", "model_id": "alternate"},
        })
        assert selected.status_code == 200, selected.text
        entered, release = nonstream_response_gate
        release.clear()
        try:
            old_accepted = await submit(old_session)
            assert await asyncio.to_thread(entered.wait, 10), "旧 Job 未到达真实模型 HTTP 入口"
            assert [header["authorization"] for header in nonstream_auth_headers] == ["Bearer " + before]
            rotated = await gateway.put(f"/api/gateway/secrets/{secret_id}", json={
                "expected_revision": 1, "name": "执行共享密钥", "value": after,
            })
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["data"]["items"][0]["revision"] == 2
            new_accepted = await submit(new_session)
            new_job = await finish(new_accepted)
            # 旧 HTTP 尚未放行，新 Thread 已按新值完成真实工具循环。
            assert [header["authorization"] for header in nonstream_auth_headers] == ["Bearer " + before, "Bearer " + after, "Bearer " + after]
            old_status = await client.get(f"/api/v1/jobs/{old_accepted['job_id']}")
            assert old_status.json()["data"]["status"] == "running"
        finally:
            release.set()
        old_job = await finish(old_accepted)
        assert [header["authorization"] for header in nonstream_auth_headers] == ["Bearer " + before, "Bearer " + after, "Bearer " + after, "Bearer " + before]
        resumed_job = await finish(await submit(old_session))
        assert nonstream_auth_headers[-1]["authorization"] == "Bearer " + after
        assert len(requests) == 5

        deleted = await gateway.delete(f"/api/gateway/secrets/{secret_id}", params={"expected_revision": 2})
        assert deleted.status_code == 200, deleted.text
        missing_job = await finish(await submit(old_session), expected="failed")
        assert "status=404" in missing_job["error_message"] and secret_id in missing_job["error_message"]
        assert len(requests) == 5, "密钥缺失时不能复用驻留模型、环境变量或隐式 fallback"
        # 每个 Job 只读取一批去重后的 primary／fallback 引用，工具循环不重复解密。
        assert resolved_ids == [[secret_id]] * 4
        definitions = await client.get(source_url)
        assert definitions.status_code == 200, definitions.text
        assert resolved_ids == [[secret_id]] * 4

    for path in (workspace / ".boxteam").rglob("*"):
        if path.is_file():
            payload = path.read_bytes()
            assert before.encode() not in payload and after.encode() not in payload, path
    (context.artifacts_dir / "credential-rotation.json").write_text(json.dumps({
        "jobs": [old_job, new_job, resumed_job, missing_job], "resolved_ids": resolved_ids,
        "http_credential_revisions": [{"Bearer " + before: 1, "Bearer " + after: 2}[header["authorization"]]
                                      for header in nonstream_auth_headers],
        "boundary": "真实 Gateway 密钥和 Skill 路由、ASGI Workspace、真实 SDK／TCP 模型协议替身",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
