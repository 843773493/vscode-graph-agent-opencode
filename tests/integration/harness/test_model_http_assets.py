"""共享响应资产经过真实本地 HTTP 和 OpenAI SDK；不启动业务服务。"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI
from openai.types.chat import ChatCompletionChunk

from app.testing.model_stream import get_protocol_codec, load_cassette
from tests.integration.stubs.http_stubs import (
    HTTPStubState,
    openai_chat_stub,
    openai_chat_tool_loop_stub,
)
from tests.support.ports import integration_port_block_for_file


@pytest.fixture(params=["text", "tool"])
def chat_http_stub(request: pytest.FixtureRequest) -> Iterator[tuple[str, HTTPStubState]]:
    port = integration_port_block_for_file(Path(request.node.path)).port(20)
    factory = openai_chat_stub if request.param == "text" else openai_chat_tool_loop_stub
    with factory(port) as state:
        yield f"http://127.0.0.1:{port}/v1", state


@pytest.mark.parametrize("async_sdk", [False, True])
async def test_http_chat_assets_preserve_sdk_text_tools_and_terminal(
    request: pytest.FixtureRequest, chat_http_stub: tuple[str, HTTPStubState], async_sdk: bool,
) -> None:
    url, state = chat_http_stub
    scenario = request.node.callspec.params["chat_http_stub"]
    reply = {"content": "原始正文 Ω", "reasoning_content": "原始推理", "finish_reason": "length"}
    state.response_callback = lambda _request: reply
    messages = [{"role": "system", "content": "验证原始上游协议"}, {"role": "user", "content": "开始"}]
    payload = {"model": "http-asset-test", "stream": True, "messages": messages}
    if async_sdk:
        async with AsyncOpenAI(base_url=url, api_key="e2e-local-model-key", max_retries=0) as client:
            stream = await client.chat.completions.create(**payload)
            chunks = [chunk async for chunk in stream]
    else:
        with (
            OpenAI(base_url=url, api_key="e2e-local-model-key", max_retries=0) as client,
            client.chat.completions.create(**payload) as stream,
        ):
            chunks = list(stream)
    choices = [chunk.choices[0] for chunk in chunks if chunk.choices]
    if scenario == "text":
        assert "".join(choice.delta.content or "" for choice in choices) == "原始正文 Ω"
        assert choices[0].delta.model_extra["reasoning_content"] == "原始推理"
        assert choices[-1].finish_reason == "length"
        assert chunks[-1].choices == []
        assert chunks[-1].usage.total_tokens == 12
        assert reply["finish_reason"] == "length", "发送不能修改调用方的场景资产"
    else:
        assert all(isinstance(chunk, ChatCompletionChunk) for chunk in chunks)
        calls = [call for choice in choices for call in choice.delta.tool_calls or []]
        assert calls[0].id == "call_browser_chat_tool_loop"
        assert calls[0].function.name == "read_file"
        assert json.loads("".join(call.function.arguments or "" for call in calls)) == {"path": "README.md"}
        assert choices[-1].finish_reason == "tool_calls"
    assert len(state.requests) == 1
    assert state.requests[0]["json"]["messages"] == messages


def test_http_chat_json_failure_and_authorization_share_scenario_asset(
    request: pytest.FixtureRequest,
) -> None:
    port = integration_port_block_for_file(Path(request.node.path)).port(21)
    reply = {"content": "JSON 正文 Ω", "finish_reason": "length"}
    with openai_chat_stub(port, failure_marker="reject-this-request") as state:
        state.response_callback = lambda _request: reply
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/v1", timeout=5, trust_env=False) as client:
            # 本项只核认证与场景资产；拒绝请求关闭本连接，不借此声称验证了 keep-alive。
            rejected = client.post("/chat/completions", json={"model": "http-asset-test"}, headers={"Connection": "close"})
            assert rejected.status_code == 401
            assert state.requests == []
            client.headers["Authorization"] = "Bearer e2e-local-model-key"
            response = client.post("/chat/completions", json={"model": "http-asset-test", "stream": False})
            assert response.status_code == 200
            choice = response.json()["choices"][0]
            assert choice["message"] == {"role": "assistant", "content": "JSON 正文 Ω"}
            assert choice["finish_reason"] == "length"
            failed = client.post("/chat/completions", json={"model": "reject-this-request", "stream": True})
            assert failed.status_code == 500
            assert failed.json()["error"]["message"] == "集成测试模型替身故障"
            assert reply == {"content": "JSON 正文 Ω", "finish_reason": "length"}
            assert len(state.requests) == 2


@pytest.fixture(scope="module", params=["responses", "chat_completions"])
def model_protocol(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def model_replay(integration_workspace_root_path: str, model_protocol: str) -> Iterator[tuple[str, list[dict[str, object]]]]:
    requests: list[dict[str, object]] = []
    protocol_directory = "openai_responses" if model_protocol == "responses" else "openai_chat"
    fixture = Path.cwd() / "tests/fixtures/model_stream/handwritten" / protocol_directory / "basic-text.json"
    response = load_cassette(fixture).interactions[0]
    codec = get_protocol_codec(response.protocol)
    codec.validate_stream(response.response.frames, label="关闭验收模型响应")
    body = b"".join(codec.encode(frame) for frame in response.response.frames)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            expected_path = "/v1/responses" if model_protocol == "responses" else "/v1/chat/completions"
            assert self.path == expected_path
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ExitStack() as lifetime:
        def save_replay_requests() -> None:
            output = Path(integration_workspace_root_path).parent / "artifacts"
            output.mkdir(exist_ok=True)
            (output / "model-replay-requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))

        lifetime.callback(save_replay_requests)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        lifetime.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def join_server_thread() -> None:
            thread.join(timeout=5)
            assert not thread.is_alive()

        lifetime.callback(join_server_thread)
        lifetime.callback(server.shutdown)
        yield f"http://127.0.0.1:{server.server_port}/v1", requests


@pytest.fixture
def shutdown_http_asset(
    model_protocol: str, model_replay: tuple[str, list[dict[str, object]]],
) -> tuple[str, str, list[dict[str, object]]]:
    # 真实网络和业务关闭验收经同一个 pytest fixture 注入资产与 socket 生命周期。
    replay_url, recorded_requests = model_replay
    recorded_requests.clear()
    return model_protocol, replay_url, recorded_requests


@pytest.mark.parametrize("async_sdk", [False, True])
async def test_shutdown_assets_reach_real_sync_and_async_sdk(
    shutdown_http_asset: tuple[str, str, list[dict[str, object]]], async_sdk: bool,
) -> None:
    protocol, url, requests = shutdown_http_asset
    if async_sdk:
        async with AsyncOpenAI(base_url=url, api_key="fixture-key", max_retries=0) as client:
            if protocol == "responses":
                stream = await client.responses.create(model="http-asset-test", input="读取资产", stream=True)
            else:
                stream = await client.chat.completions.create(
                    model="http-asset-test", messages=[{"role": "user", "content": "读取资产"}], stream=True,
                )
            events = [event async for event in stream]
    else:
        with OpenAI(base_url=url, api_key="fixture-key", max_retries=0) as client:
            if protocol == "responses":
                stream = client.responses.create(model="http-asset-test", input="读取资产", stream=True)
            else:
                stream = client.chat.completions.create(
                    model="http-asset-test", messages=[{"role": "user", "content": "读取资产"}], stream=True,
                )
            with stream:
                events = list(stream)
    if protocol == "responses":
        assert events[-1].type == "response.completed"
        assert events[-1].response.output_text == "响应文本"
        assert "".join(event.delta for event in events if event.type == "response.output_text.delta") == "响应文本"
    else:
        assert "".join(event.choices[0].delta.content or "" for event in events if event.choices) == "基础回放"
        assert next(event.choices[0].finish_reason for event in events if event.choices and event.choices[0].finish_reason) == "stop"
    assert len(requests) == 1
    assert requests[0]["model"] == "http-asset-test"
    assert requests[0]["stream"] is True
