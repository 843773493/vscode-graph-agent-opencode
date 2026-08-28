from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.testing.model_stream import (
    ModelStreamAssetError,
    ModelStreamHTTPTransport,
    ModelStreamMatchError,
    StreamScenario,
    get_protocol_codec,
    load_cassette,
    load_cassette_from_object,
    load_scenario,
    replay_session,
)
from app.testing.model_stream.matcher import request_summary, safe_request_match_fields

FIXTURE_ROOT = Path.cwd() / "tests" / "fixtures" / "model_stream"


class _FakeStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], error: BaseException | None = None) -> None:
        self._chunks = chunks
        self._error = error

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        return None


class _FakeLiveTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            status_code=200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "request-1",
                "set-cookie": "secret-cookie",
            },
            stream=self._stream,
            request=request,
        )

    async def aclose(self) -> None:
        return None


def _request_json(model: str = "big-pickle") -> dict[str, object]:
    return {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": "测试"}],
    }


def test_request_summary_keeps_chat_message_roles_without_message_content() -> None:
    request = httpx.Request(
        "POST",
        "https://provider.example/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [
                {"role": "system", "content": "内部提示"},
                {"role": "user", "content": "用户问题"},
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "read_file"}}],
                },
                {"role": "tool", "content": "工具输出"},
            ],
        },
    )

    summary = request_summary(request)

    assert summary.selected_body["message_roles"] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert "内部提示" not in str(summary.selected_body)
    assert "用户问题" not in str(summary.selected_body)
    assert "工具输出" not in str(summary.selected_body)
    assert safe_request_match_fields(request)["message_roles"] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]


def _sequence_cassette():
    response_frames = lambda text: [
        {
            "kind": "data",
            "encoding": "json",
            "payload": {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ]
            },
        },
        {
            "kind": "data",
            "encoding": "json",
            "payload": {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ]
            },
        },
        {"kind": "done", "encoding": "text", "payload": "[DONE]"},
    ]
    return load_cassette_from_object(
        {
            "schema_version": 1,
            "kind": "model_stream_cassette",
            "metadata": {
                "source": "handwritten",
                "asset_id": "sequence-test",
                "protocol": "openai_chat_sse",
            },
            "interactions": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://provider.example/v1/chat/completions",
                        "match": {"model": "test-model", "stream": True},
                    },
                    "replay": {"sequence_id": "tool-loop", "step": 0},
                    "response": {
                        "status": 200,
                        "headers": {"content-type": "text/event-stream"},
                        "frames": response_frames("第一步"),
                    },
                },
                {
                    "request": {
                        "method": "POST",
                        "url": "https://provider.example/v1/chat/completions",
                        "match": {"model": "test-model", "stream": True},
                    },
                    "replay": {"sequence_id": "tool-loop", "step": 1},
                    "response": {
                        "status": 200,
                        "headers": {"content-type": "text/event-stream"},
                        "frames": response_frames("第二步"),
                    },
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_request_reusable_creates_independent_streams(tmp_path: Path) -> None:
    scenario = load_scenario(FIXTURE_ROOT, "basic-text")
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="replay",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "https://opencode.ai/zen/v1/chat/completions",
                    json=_request_json(),
                )
                for _ in range(24)
            )
        )

    expected = (
        json.dumps(
            {
                "id": "chatcmpl-basic-text",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "big-pickle",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "基础"},
                        "finish_reason": None,
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        .join(("data: ", "\n\n"))
        .encode()
    )
    assert all(response.status_code == 200 for response in responses)
    bodies = []
    for response in responses:
        bodies.append(await response.aread())
    assert all(body.startswith(expected) for body in bodies)
    assert transport.call_count == 24
    assert transport.hit_counts == {0: 24}


@pytest.mark.asyncio
async def test_unmatched_request_does_not_leak_sensitive_data(tmp_path: Path) -> None:
    scenario = load_scenario(FIXTURE_ROOT, "basic-text")
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="replay",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ModelStreamMatchError) as error:
            await client.post(
                "https://opencode.ai/zen/v1/chat/completions?api_key=secret-key",
                headers={
                    "Authorization": "Bearer secret-key",
                    "X-Request-ID": "replay-diagnostic-1",
                },
                json=_request_json(model="unknown-model"),
            )

    assert "secret-key" not in str(error.value)
    assert "unknown-model" in str(error.value)
    assert "replay-diagnostic-1" in str(error.value)
    assert "candidates=" in str(error.value)


@pytest.mark.asyncio
async def test_session_sequence_has_a_cursor_per_explicit_session(tmp_path: Path) -> None:
    cassette = _sequence_cassette()
    scenario = StreamScenario(
        scenario_id="sequence-test",
        asset_path=Path("<memory>"),
        cassette=cassette,
        business_assertion=None,
        raw={},
    )
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="replay",
        replay_policy="session_sequence",
        artifact_root=tmp_path,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with replay_session("session-a"):
            first_a = await client.post(
                "https://provider.example/v1/chat/completions",
                json={"model": "test-model", "stream": True},
            )
            second_a = await client.post(
                "https://provider.example/v1/chat/completions",
                json={"model": "test-model", "stream": True},
            )
        with replay_session("session-b"):
            first_b = await client.post(
                "https://provider.example/v1/chat/completions",
                json={"model": "test-model", "stream": True},
            )

    assert "第一步".encode() in await first_a.aread()
    assert "第二步".encode() in await second_a.aread()
    assert "第一步".encode() in await first_b.aread()
    assert transport.hit_counts == {0: 2, 1: 1}


@pytest.mark.asyncio
async def test_session_sequence_requires_explicit_session(tmp_path: Path) -> None:
    cassette = _sequence_cassette()
    scenario = StreamScenario(
        scenario_id="sequence-test",
        asset_path=Path("<memory>"),
        cassette=cassette,
        business_assertion=None,
        raw={},
    )
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="replay",
        replay_policy="session_sequence",
        artifact_root=tmp_path,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ModelStreamMatchError, match="replay_session_id"):
            await client.post(
                "https://provider.example/v1/chat/completions",
                json={"model": "test-model", "stream": True},
            )


@pytest.mark.asyncio
async def test_record_forwards_original_bytes_and_writes_complete_cassette(
    tmp_path: Path,
) -> None:
    chunks = (
        'data: {"id":"recorded","choices":[{"delta":{"content":"录"}}]}\n'.encode(),
        '\ndata: {"choices":[{"delta":{"content":"制"}}]}\n\n'.encode(),
        b"data: [DONE]\n\n",
    )
    live_transport = _FakeLiveTransport(_FakeStream(chunks))
    scenario = load_scenario(FIXTURE_ROOT, "basic-text")
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="record",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
        live_transport=live_transport,
    )
    request_body = _request_json()
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post(
            "https://opencode.ai/zen/v1/chat/completions?api_key=secret-key&region=west",
            json=request_body,
        )

    wire_body = await response.aread()
    assert wire_body == b"".join(chunks)
    artifacts = tuple(tmp_path.glob("basic-text-*.json"))
    assert len(artifacts) == 1
    cassette = load_cassette(artifacts[0])
    assert cassette.metadata["source"] == "recorded"
    recorded_url = cassette.interactions[0].request.url
    assert "secret-key" not in recorded_url
    assert recorded_url.endswith("?api_key=%5BREDACTED%5D&region=west")
    assert "secret-key" not in artifacts[0].read_text(encoding="utf-8")
    assert [frame.payload for frame in cassette.interactions[0].response.frames] == [
        {"id": "recorded", "choices": [{"delta": {"content": "录"}}]},
        {"choices": [{"delta": {"content": "制"}}]},
        "[DONE]",
    ]
    assert "set-cookie" not in cassette.interactions[0].response.headers
    assert live_transport.requests[0].url == response.request.url

    replay_scenario = StreamScenario(
        scenario_id="recorded-replay",
        asset_path=artifacts[0],
        cassette=cassette,
        business_assertion=None,
        raw={},
    )
    replay_transport = ModelStreamHTTPTransport(
        scenario=replay_scenario,
        mode="replay",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
    )
    async with httpx.AsyncClient(transport=replay_transport) as replay_client:
        replay_response = await replay_client.post(
            "https://opencode.ai/zen/v1/chat/completions?api_key=another-secret&region=west",
            json=request_body,
        )
    assert await replay_response.aread() == wire_body


@pytest.mark.asyncio
async def test_record_responses_preserves_event_names_and_original_bytes(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(FIXTURE_ROOT, "responses-basic-text")
    codec = get_protocol_codec("openai_responses_sse")
    source_frames = scenario.cassette.interactions[0].response.frames
    wire_body = b"".join(codec.encode(frame) for frame in source_frames)
    live_transport = _FakeLiveTransport(
        _FakeStream((wire_body[:73], wire_body[73:]))
    )
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="record",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
        live_transport=live_transport,
    )

    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post(
            "https://www.cctq.ai/v1/responses",
            json={
                "model": "gpt-5.6-luna",
                "stream": True,
                "input": [{"type": "message", "role": "user", "content": "测试"}],
            },
        )
        assert await response.aread() == wire_body

    artifacts = tuple(tmp_path.glob("responses-basic-text-*.json"))
    assert len(artifacts) == 1
    recorded = load_cassette(artifacts[0])
    assert [
        frame.event for frame in recorded.interactions[0].response.frames
    ] == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert recorded.interactions[0].request.match == {
        "model": "gpt-5.6-luna",
        "stream": True,
        "input_types": ["message"],
    }


@pytest.mark.asyncio
async def test_record_marks_incomplete_stream_without_promoting_cassette(
    tmp_path: Path,
) -> None:
    live_transport = _FakeLiveTransport(
        _FakeStream(
            (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',),
            error=RuntimeError("upstream disconnected"),
        )
    )
    scenario = load_scenario(FIXTURE_ROOT, "basic-text")
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="record",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
        live_transport=live_transport,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="upstream disconnected"):
            await client.post(
                "https://opencode.ai/zen/v1/chat/completions",
                json=_request_json(),
            )

    assert tuple(
        path for path in tmp_path.glob("basic-text-*.json")
        if not path.name.endswith(".incomplete.json")
    ) == ()
    incomplete = tuple(tmp_path.glob("basic-text-*.incomplete.json"))
    assert len(incomplete) == 1
    diagnostic = json.loads(incomplete[0].read_text(encoding="utf-8"))
    assert diagnostic["kind"] == "model_stream_incomplete"
    assert diagnostic["saw_done"] is False


@pytest.mark.asyncio
async def test_record_rejects_unterminated_sse_frame_as_incomplete(
    tmp_path: Path,
) -> None:
    live_transport = _FakeLiveTransport(
        _FakeStream((b'data: {"choices":[]}',))
    )
    scenario = load_scenario(FIXTURE_ROOT, "basic-text")
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="record",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
        live_transport=live_transport,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ModelStreamAssetError, match="未完成的 SSE frame"):
            await client.post(
                "https://opencode.ai/zen/v1/chat/completions",
                json=_request_json(),
            )

    assert tuple(
        path for path in tmp_path.glob("basic-text-*.json")
        if not path.name.endswith(".incomplete.json")
    ) == ()
    assert len(tuple(tmp_path.glob("basic-text-*.incomplete.json"))) == 1


@pytest.mark.asyncio
async def test_record_cancellation_writes_incomplete_diagnostic(tmp_path: Path) -> None:
    live_transport = _FakeLiveTransport(
        _FakeStream((b'data: {"choices":[]}',))
    )
    scenario = load_scenario(FIXTURE_ROOT, "basic-text")
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="record",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
        live_transport=live_transport,
    )
    async with httpx.AsyncClient(transport=transport) as client, client.stream(
        "POST",
        "https://opencode.ai/zen/v1/chat/completions",
        json=_request_json(),
    ) as response:
        first_chunk = await response.aiter_bytes().__anext__()
        assert first_chunk == b'data: {"choices":[]}'

    assert tuple(
        path for path in tmp_path.glob("basic-text-*.json")
        if not path.name.endswith(".incomplete.json")
    ) == ()
    assert len(tuple(tmp_path.glob("basic-text-*.incomplete.json"))) == 1


@pytest.mark.asyncio
async def test_record_rejects_invalid_utf8_sse_frame(tmp_path: Path) -> None:
    live_transport = _FakeLiveTransport(_FakeStream((b"data: \xff\n\n",)))
    scenario = load_scenario(FIXTURE_ROOT, "basic-text")
    transport = ModelStreamHTTPTransport(
        scenario=scenario,
        mode="record",
        replay_policy="request_reusable",
        artifact_root=tmp_path,
        live_transport=live_transport,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UnicodeDecodeError):
            await client.post(
                "https://opencode.ai/zen/v1/chat/completions",
                json=_request_json(),
            )

    assert len(tuple(tmp_path.glob("basic-text-*.incomplete.json"))) == 1
