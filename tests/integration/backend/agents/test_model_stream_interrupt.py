from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
import litellm
import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)
from app.core.session_paths import SessionPathResolver
from app.core.turn_execution_scope import (
    AgentControlInbox,
    AgentLoopControlCoordinator,
    ScopeCancelledError,
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.services.infrastructure.message_stream_store import (
    MessageStreamNotFoundError,
    MessageStreamStore,
    MessageStreamSubscription,
    MessageStreamTerminalError,
)
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from app.testing.model_stream import StreamFrame, get_protocol_codec, load_scenario

ProviderKind = Literal["chat", "responses"]
InterruptStage = Literal["reasoning", "text", "tool_call"]

FIXTURE_ROOT = Path.cwd() / "tests" / "fixtures" / "model_stream"
OUTPUT_ROOT = (
    Path.cwd()
    / "out/tests/integration/backend/agents/test_model_stream_interrupt"
)


class _GatedSSEStream(httpx.AsyncByteStream):
    """按 SSE frame 输出并在目标 frame 后阻塞，模拟 provider 持续推流。"""

    def __init__(
        self,
        frames: Sequence[bytes],
        *,
        pause_after: int | None,
    ) -> None:
        self._frames = tuple(frames)
        self._pause_after = pause_after
        self._closed = False
        self._release = asyncio.Event()
        self.closed = asyncio.Event()
        self.read_waiting = asyncio.Event()
        self.read_cancelled = False
        self.read_calls = 0
        self.yielded_frame_count = 0
        self.close_count = 0

    def __aiter__(self) -> _GatedSSEStream:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        frame_index = self.read_calls
        self.read_calls += 1
        if frame_index >= len(self._frames):
            raise StopAsyncIteration
        if self._pause_after is not None and frame_index == self._pause_after + 1:
            self.read_waiting.set()
            try:
                await self._release.wait()
            except asyncio.CancelledError:
                self.read_cancelled = True
                raise
        if self._closed:
            raise StopAsyncIteration
        self.yielded_frame_count += 1
        return self._frames[frame_index]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self._release.set()
        self.closed.set()


class _SSETransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: _GatedSSEStream) -> None:
        self.stream = stream
        self.request_urls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_urls.append(str(request.url))
        return httpx.Response(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream=self.stream,
            request=request,
        )

    async def aclose(self) -> None:
        await self.stream.aclose()


class _TCPGatedSSEServer:
    """真实 TCP SSE 服务，发送目标 frame 后等待客户端关闭连接。"""

    def __init__(self, frames: Sequence[bytes], *, pause_after: int) -> None:
        self._frames = tuple(frames)
        self._pause_after = pause_after
        self._server: asyncio.Server | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._endpoint: str | None = None
        self.target_sent = asyncio.Event()
        self.client_closed = asyncio.Event()
        self.server_closed = asyncio.Event()
        self.sent_frame_count = 0
        self.connection_count = 0

    @property
    def endpoint(self) -> str:
        if self._endpoint is None:
            raise RuntimeError("TCP SSE server 尚未启动")
        return self._endpoint

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection,
            host="127.0.0.1",
            port=0,
        )
        socket = self._server.sockets
        if not socket:
            raise RuntimeError("TCP SSE server 未分配监听 socket")
        address = socket[0].getsockname()
        self._endpoint = f"http://127.0.0.1:{address[1]}/v1"

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        self.connection_count += 1
        try:
            request_headers = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for line in request_headers.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
                    break
            if content_length:
                await reader.readexactly(content_length)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
            )
            await writer.drain()
            for index, frame in enumerate(self._frames):
                writer.write(frame)
                await writer.drain()
                self.sent_frame_count = index + 1
                if index == self._pause_after:
                    self.target_sent.set()
                    await reader.read()
                    self.client_closed.set()
                    return
        except (
            asyncio.IncompleteReadError,
            BrokenPipeError,
            ConnectionResetError,
        ):
            self.client_closed.set()
        finally:
            writer.close()
            await writer.wait_closed()
            self.server_closed.set()
            if task is not None:
                self._handler_tasks.discard(task)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        pending = tuple(self._handler_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class _BlockDeltaBarrierStore(MessageStreamStore):
    """在首个 block.delta 落盘前暂停，制造提交与 interrupt 的竞态。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.delta_commit_started = asyncio.Event()
        self.release_delta_commit = asyncio.Event()
        self._barrier_used = False

    async def commit(
        self,
        turn_stream_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        model_call_id: str | None = None,
        block_id: str | None = None,
        tool_execution_id: str | None = None,
        job_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if event_type == "block.delta" and not self._barrier_used:
            self._barrier_used = True
            self.delta_commit_started.set()
            await self.release_delta_commit.wait()
        return await super().commit(
            turn_stream_id,
            event_type,
            payload,
            model_call_id=model_call_id,
            block_id=block_id,
            tool_execution_id=tool_execution_id,
            job_id=job_id,
            event_id=event_id,
        )


@pytest.fixture
def message_stream_context() -> tuple[
    MessageStreamStore,
    SessionPathResolver,
    str,
]:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    sessions_root = OUTPUT_ROOT / "workspace" / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_root)
    resolver.initialize()
    session_id = "ses_provider_interrupt_integration"
    session_dir = resolver.allocate_session_dir(
        session_id=session_id,
        title=session_id,
    )
    now = "2026-08-27T00:00:00Z"
    (session_dir / "session.json").write_text(
        f'{{"session_id":"{session_id}","title":"{session_id}",'
        f'"created_at":"{now}","updated_at":"{now}"}}',
        encoding="utf-8",
    )
    resolver.register_session(session_id, session_dir)
    return MessageStreamStore(path_resolver=resolver), resolver, session_id


def _scenario_frames(
    provider: ProviderKind,
    stage: InterruptStage | Literal["tool_execution"],
) -> tuple[StreamFrame, ...]:
    if provider == "chat":
        scenario_id = "reasoning-tool" if stage in {"tool_call", "tool_execution"} else (
            "reasoning-stream" if stage == "reasoning" else "basic-text"
        )
    else:
        scenario_id = (
            "responses-reasoning-tool"
            if stage in {"tool_call", "tool_execution"}
            else "responses-reasoning-text"
        )
    scenario = load_scenario(FIXTURE_ROOT, scenario_id)
    return scenario.cassette.interactions[0].response.frames


def _frame_phase(frame: StreamFrame, provider: ProviderKind) -> str | None:
    if provider == "responses":
        return {
            "response.reasoning_summary_text.delta": "reasoning",
            "response.output_text.delta": "text",
            "response.function_call_arguments.delta": "tool_call",
        }.get(frame.event or "")
    payload = frame.payload
    if not isinstance(payload, Mapping):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    delta = choice.get("delta")
    if not isinstance(delta, Mapping):
        return None
    if delta.get("reasoning_content"):
        return "reasoning"
    if delta.get("content"):
        return "text"
    if delta.get("tool_calls"):
        return "tool_call"
    return None


def _target_frame_index(
    frames: Sequence[StreamFrame],
    *,
    provider: ProviderKind,
    stage: InterruptStage,
) -> int:
    for index, frame in enumerate(frames):
        if _frame_phase(frame, provider) == stage:
            return index
    raise AssertionError(
        f"手写 SSE fixture 缺少目标阶段: provider={provider} stage={stage}"
    )


def _model(provider: ProviderKind, *, api_base: str | None = None):
    if provider == "chat":
        return BoxteamLiteLLMChatModel(
            model="openai/big-pickle",
            api_key="test-key",
            api_base=api_base or "https://opencode.ai/zen/v1",
            custom_llm_provider="openai",
            streaming=True,
        )
    return BoxteamOpenAIResponsesModel(
        model="gpt-5.6-luna",
        api_key="test-key",
        api_base=api_base or "https://www.cctq.ai/v1",
        custom_llm_provider="openai",
        responses_store=False,
        streaming=True,
    )


def _protocol(provider: ProviderKind) -> Literal[
    "openai_chat_sse", "openai_responses_sse"
]:
    return "openai_chat_sse" if provider == "chat" else "openai_responses_sse"


def _install_litellm_client(
    client: httpx.AsyncClient,
) -> tuple[httpx.AsyncClient | None, object]:
    previous_session = litellm.aclient_session
    if previous_session is not None:
        raise RuntimeError("测试开始前 LiteLLM async session 已存在")
    injected_handler = AsyncHTTPHandler.__new__(AsyncHTTPHandler)
    injected_handler.timeout = None
    injected_handler.event_hooks = None
    injected_handler.client_alias = "boxteam-provider-interrupt"
    injected_handler.client = client
    previous_factory = litellm_http_handler.get_async_httpx_client

    def get_injected_async_client(
        *_args: object,
        **_kwargs: object,
    ) -> AsyncHTTPHandler:
        return injected_handler

    litellm_http_handler.get_async_httpx_client = get_injected_async_client
    litellm.aclient_session = client
    return previous_session, previous_factory


def _restore_litellm_client(
    client: httpx.AsyncClient,
    previous_session: httpx.AsyncClient | None,
    previous_factory: object,
) -> None:
    litellm_http_handler.get_async_httpx_client = previous_factory
    if litellm.aclient_session is client:
        litellm.aclient_session = previous_session


async def _read_until(
    subscription: MessageStreamSubscription,
    observed: list[dict[str, object]],
    predicate,
    *,
    producer_task: asyncio.Task[None] | None = None,
) -> dict[str, object]:
    while True:
        if producer_task is not None and producer_task.done():
            producer_task.result()
        record = await asyncio.wait_for(subscription.get(), timeout=2)
        event = record.event
        observed.append(event)
        if predicate(event):
            return event


async def _read_reconnect_terminal_event(
    store: MessageStreamStore,
    *,
    session_id: str,
    turn_stream_id: str,
    after_seq: int,
) -> dict[str, object]:
    replay = store.stream_records(
        session_id=session_id,
        turn_stream_id=turn_stream_id,
        after_seq=after_seq,
    )
    try:
        return await asyncio.wait_for(anext(replay), timeout=2)
    finally:
        await replay.aclose()


async def _wait_for_event_on_disk(
    resolver: SessionPathResolver,
    *,
    session_id: str,
    turn_stream_id: str,
    event_type: str,
) -> list[dict[str, object]]:
    for _attempt in range(2000):
        probe = MessageStreamStore(path_resolver=resolver)
        try:
            events = await probe.list_events(
                session_id=session_id,
                turn_stream_id=turn_stream_id,
            )
        except MessageStreamNotFoundError:
            events = []
        if any(event.get("type") == event_type for event in events):
            return events
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"等待消息流事件超时: turn_stream_id={turn_stream_id} event_type={event_type}"
    )


async def _wait_for_worker_ready(
    path: Path,
    worker: asyncio.subprocess.Process,
) -> None:
    for _attempt in range(2000):
        if path.is_file():
            return
        if worker.returncode is not None:
            stderr = ""
            if worker.stderr is not None:
                stderr = (await worker.stderr.read()).decode(
                    "utf-8",
                    errors="replace",
                )
            raise AssertionError(
                "crash worker 提前退出: "
                f"returncode={worker.returncode}, stderr={stderr}"
            )
        await asyncio.sleep(0.01)
    raise AssertionError(f"等待跨进程就绪标记超时: {path}")


def _event_payload(event: Mapping[str, object]) -> Mapping[str, object]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError(f"消息流事件 payload 不是对象: {event!r}")
    return payload


async def _consume_model(model) -> None:
    async for _chunk in model._astream([HumanMessage(content="测试中断")]):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "stage"),
    [
        ("chat", "reasoning"),
        ("chat", "text"),
        ("chat", "tool_call"),
        ("responses", "reasoning"),
        ("responses", "text"),
        ("responses", "tool_call"),
    ],
)
async def test_handwritten_provider_interrupt_stops_pending_sse_read(
    message_stream_context: tuple[MessageStreamStore, SessionPathResolver, str],
    provider: ProviderKind,
    stage: InterruptStage,
) -> None:
    store, resolver, session_id = message_stream_context
    frames = _scenario_frames(provider, stage)
    protocol = _protocol(provider)
    codec = get_protocol_codec(protocol)
    wire_frames = tuple(codec.encode(frame) for frame in frames)
    target_index = _target_frame_index(frames, provider=provider, stage=stage)
    upstream = _GatedSSEStream(wire_frames, pause_after=target_index)
    transport = _SSETransport(upstream)
    client = httpx.AsyncClient(transport=transport)
    previous_session, previous_factory = _install_litellm_client(client)

    writer = await store.open(
        session_id=session_id,
        turn_id=f"job_provider_interrupt_{provider}_{stage}",
        job_id=f"job_provider_interrupt_{provider}_{stage}",
    )
    subscription = await store.subscribe(writer.turn_stream_id)
    runtime = MessageStreamRuntime(writer)
    scope = TurnExecutionScope(writer.turn_stream_id)
    scope_token = set_current_turn_execution_scope(scope)
    sink_token = set_current_model_delta_sink(runtime)
    model_task: asyncio.Task[None] | None = None
    observed: list[dict[str, object]] = []
    try:
        await runtime.start_model(f"model_{provider}_{stage}", provider)
        model_task = asyncio.create_task(_consume_model(_model(provider)))
        target_event = await _read_until(
            subscription,
            observed,
            lambda event: (
                event.get("type") == "block.delta"
                and _event_payload(event).get("carrier_type")
                in (
                    {"reasoning", "reasoning_content", "thinking"}
                    if stage == "reasoning"
                    else {"text", "output_text", "refusal"}
                )
            )
            if stage != "tool_call"
            else event.get("type") == "tool_call.delta",
            producer_task=model_task,
        )
        if stage == "tool_call":
            assert _event_payload(target_event)["tool_call_id"]
        await asyncio.wait_for(upstream.read_waiting.wait(), timeout=2)
        assert upstream.yielded_frame_count == target_index + 1

        inbox = AgentControlInbox(writer.turn_stream_id)
        coordinator = AgentLoopControlCoordinator(
            scope=scope,
            inbox=inbox,
            writer=writer,
        )
        command_id = f"interrupt_{provider}_{stage}"
        command = inbox.accept(
            command_id=command_id,
            kind="interrupt",
            idempotency_key=command_id,
            payload={"reason": "user_requested"},
        )
        interrupt_event = await coordinator.process(command)
        assert interrupt_event["type"] == "interrupt.requested"

        with pytest.raises(ScopeCancelledError, match="user_requested"):
            await asyncio.wait_for(model_task, timeout=2)
        await asyncio.wait_for(upstream.closed.wait(), timeout=2)
        assert upstream.read_cancelled is True
        assert upstream.close_count == 1
        assert upstream.yielded_frame_count == target_index + 1
        assert upstream.read_calls == target_index + 2

        await runtime.finalize_interruption_facts()
        await writer.close_interrupted(command_id)
        await _read_until(
            subscription,
            observed,
            lambda event: event.get("type") == "stream.interrupted",
        )

        events = await store.list_events(
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
        )
        assert [event["event_seq"] for event in observed] == [
            event["event_seq"] for event in events[1:]
        ]
        assert [event["event_seq"] for event in events] == list(
            range(1, len(events) + 1)
        )
        event_types = [str(event["type"]) for event in events]
        interrupt_index = event_types.index("interrupt.requested")
        assert event_types[-1] == "stream.interrupted"
        assert event_types.index("model.failed") > interrupt_index
        if stage in {"reasoning", "text"}:
            assert any(
                index > interrupt_index
                for index, event_type in enumerate(event_types)
                if event_type == "block.completed"
            )
        else:
            assert event_types.index("tool_call.completed") > interrupt_index

        state = await store.get_state(writer.turn_stream_id)
        assert state["stream_status"] == "interrupted"
        assert state["failure"]["outcome"] == "user_interrupt"
        assert state["failure"]["retryable"] is False
        if stage in {"reasoning", "text"}:
            target_carriers = (
                {"reasoning", "reasoning_content", "thinking"}
                if stage == "reasoning"
                else {"text", "output_text", "refusal"}
            )
            assert any(
                block.get("carrier_type") in target_carriers
                and block.get("partial") is True
                for block in state["blocks"]
            )
        else:
            assert state["tool_calls"][0]["status"] in {"incomplete", "cancelled"}

        restarted = MessageStreamStore(path_resolver=resolver)
        restarted_writer = await restarted.open_existing(
            session_id=session_id,
            turn_id=writer.turn_id,
            turn_stream_id=writer.turn_stream_id,
        )
        snapshot = await restarted_writer.snapshot()
        assert snapshot["event_seq"] == events[-1]["event_seq"]
        assert snapshot["payload"]["snapshot_seq"] == events[-1]["event_seq"]
        assert snapshot["payload"]["stream_status"] == "interrupted"
        assert snapshot["payload"]["failure"]["outcome"] == "user_interrupt"
        assert snapshot["payload"]["active_state"]["kind"] == "terminal"
        reconnect_event = await _read_reconnect_terminal_event(
            restarted,
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
            after_seq=int(events[-1]["event_seq"]) - 1,
        )
        assert reconnect_event["type"] == "stream.interrupted"
        assert reconnect_event["event_seq"] == events[-1]["event_seq"]
    finally:
        if model_task is not None and not model_task.done():
            await scope.cancel("test_cleanup")
            model_task.cancel()
            await asyncio.gather(model_task, return_exceptions=True)
        reset_current_model_delta_sink(sink_token)
        reset_current_turn_execution_scope(scope_token)
        await store.unsubscribe(subscription)
        await scope.close()
        _restore_litellm_client(client, previous_session, previous_factory)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chat", "responses"])
async def test_handwritten_provider_tool_execution_interrupt_projects_unknown_result(
    message_stream_context: tuple[MessageStreamStore, SessionPathResolver, str],
    provider: ProviderKind,
) -> None:
    store, resolver, session_id = message_stream_context
    frames = _scenario_frames(provider, "tool_execution")
    codec = get_protocol_codec(_protocol(provider))
    upstream = _GatedSSEStream(
        tuple(codec.encode(frame) for frame in frames),
        pause_after=None,
    )
    transport = _SSETransport(upstream)
    client = httpx.AsyncClient(transport=transport)
    previous_session, previous_factory = _install_litellm_client(client)

    writer = await store.open(
        session_id=session_id,
        turn_id=f"job_provider_tool_interrupt_{provider}",
        job_id=f"job_provider_tool_interrupt_{provider}",
    )
    subscription = await store.subscribe(writer.turn_stream_id)
    runtime = MessageStreamRuntime(writer)
    scope = TurnExecutionScope(writer.turn_stream_id)
    scope_token = set_current_turn_execution_scope(scope)
    sink_token = set_current_model_delta_sink(runtime)
    model_task: asyncio.Task[None] | None = None
    observed: list[dict[str, object]] = []
    tool_scope: TurnExecutionScope | None = None
    tool_task: asyncio.Task[str] | None = None
    try:
        await runtime.start_model(f"model_{provider}_tool_execution", provider)
        model_task = asyncio.create_task(_consume_model(_model(provider)))
        await asyncio.wait_for(model_task, timeout=2)
        await runtime.finish_model()
        await asyncio.wait_for(upstream.closed.wait(), timeout=2)
        assert upstream.close_count == 1
        assert upstream.yielded_frame_count == len(frames)
        assert upstream.read_cancelled is False

        tool_call_id = runtime.claim_tool_call_id(
            "read_file",
            {"path": "README.md"},
        )
        assert tool_call_id is not None
        tool_scope = scope.child("tool-execution")
        await runtime.start_tool(
            tool_execution_id="exec_provider_tool_interrupt",
            tool_call_id=tool_call_id,
            tool_name="read_file",
        )
        await _read_until(
            subscription,
            observed,
            lambda event: event.get("type") == "tool.started",
        )

        async def wait_for_tool_cancellation() -> str:
            return await tool_scope.cancellation_signal.wait()

        tool_task = asyncio.create_task(wait_for_tool_cancellation())
        command_id = f"interrupt_{provider}_tool_execution"
        inbox = AgentControlInbox(writer.turn_stream_id)
        coordinator = AgentLoopControlCoordinator(
            scope=scope,
            inbox=inbox,
            writer=writer,
        )
        command = inbox.accept(
            command_id=command_id,
            kind="interrupt",
            idempotency_key=command_id,
            payload={"reason": "user_requested"},
        )
        interrupt_event = await coordinator.process(command)
        assert interrupt_event["type"] == "interrupt.requested"
        assert await asyncio.wait_for(tool_task, timeout=2) == "user_requested"

        await runtime.finalize_interruption_facts()
        await writer.close_interrupted(command_id)
        await _read_until(
            subscription,
            observed,
            lambda event: event.get("type") == "stream.interrupted",
        )

        events = await store.list_events(
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
        )
        assert [event["event_seq"] for event in observed] == [
            event["event_seq"] for event in events[1:]
        ]
        event_types = [str(event["type"]) for event in events]
        interrupt_index = event_types.index("interrupt.requested")
        assert event_types[-1] == "stream.interrupted"
        assert event_types.index("tool.completed") > interrupt_index
        assert event_types.index("model.failed") > interrupt_index

        state = await store.get_state(writer.turn_stream_id)
        assert state["stream_status"] == "interrupted"
        assert state["tool_executions"][0]["status"] == "completed"
        assert state["tool_executions"][0]["outcome"] == "outcome_unknown"
        assert state["tool_executions"][0]["completion_reason"] == "provider_failed"

        restarted = MessageStreamStore(path_resolver=resolver)
        restarted_writer = await restarted.open_existing(
            session_id=session_id,
            turn_id=writer.turn_id,
            turn_stream_id=writer.turn_stream_id,
        )
        snapshot = await restarted_writer.snapshot()
        assert snapshot["event_seq"] == events[-1]["event_seq"]
        assert snapshot["payload"]["snapshot_seq"] == events[-1]["event_seq"]
        snapshot_execution = snapshot["payload"]["tool_executions"][0]
        assert snapshot_execution["outcome"] == "outcome_unknown"
        assert snapshot_execution["status"] == "completed"
        assert snapshot["payload"]["active_state"]["kind"] == "terminal"
        reconnect_event = await _read_reconnect_terminal_event(
            restarted,
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
            after_seq=int(events[-1]["event_seq"]) - 1,
        )
        assert reconnect_event["type"] == "stream.interrupted"
        assert reconnect_event["event_seq"] == events[-1]["event_seq"]
    finally:
        if tool_task is not None and not tool_task.done():
            await scope.cancel("test_cleanup")
            await asyncio.gather(tool_task, return_exceptions=True)
        if model_task is not None and not model_task.done():
            model_task.cancel()
            await asyncio.gather(model_task, return_exceptions=True)
        reset_current_model_delta_sink(sink_token)
        reset_current_turn_execution_scope(scope_token)
        await store.unsubscribe(subscription)
        await scope.close()
        _restore_litellm_client(client, previous_session, previous_factory)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chat", "responses"])
async def test_handwritten_provider_interrupt_closes_real_tcp_sse_connection(
    message_stream_context: tuple[MessageStreamStore, SessionPathResolver, str],
    provider: ProviderKind,
) -> None:
    """真实 TCP 连接在 pending read 时必须被客户端主动关闭。"""
    store, _, session_id = message_stream_context
    frames = _scenario_frames(provider, "reasoning")
    codec = get_protocol_codec(_protocol(provider))
    wire_frames = tuple(codec.encode(frame) for frame in frames)
    target_index = _target_frame_index(frames, provider=provider, stage="reasoning")
    server = _TCPGatedSSEServer(wire_frames, pause_after=target_index)
    await server.start()
    client = httpx.AsyncClient()
    previous_session, previous_factory = _install_litellm_client(client)

    writer = await store.open(
        session_id=session_id,
        turn_id=f"job_provider_tcp_interrupt_{provider}",
        job_id=f"job_provider_tcp_interrupt_{provider}",
    )
    subscription = await store.subscribe(writer.turn_stream_id)
    runtime = MessageStreamRuntime(writer)
    scope = TurnExecutionScope(writer.turn_stream_id)
    scope_token = set_current_turn_execution_scope(scope)
    sink_token = set_current_model_delta_sink(runtime)
    model_task: asyncio.Task[None] | None = None
    try:
        await runtime.start_model(f"model_tcp_{provider}", provider)
        model_task = asyncio.create_task(
            _consume_model(_model(provider, api_base=server.endpoint))
        )
        await _read_until(
            subscription,
            [],
            lambda event: event.get("type") == "block.delta",
            producer_task=model_task,
        )
        await asyncio.wait_for(server.target_sent.wait(), timeout=2)
        assert server.sent_frame_count == target_index + 1
        assert server.connection_count == 1

        inbox = AgentControlInbox(writer.turn_stream_id)
        coordinator = AgentLoopControlCoordinator(
            scope=scope,
            inbox=inbox,
            writer=writer,
        )
        command = inbox.accept(
            command_id=f"tcp_interrupt_{provider}",
            kind="interrupt",
            idempotency_key=f"tcp_interrupt_{provider}",
            payload={"reason": "user_requested"},
        )
        interrupt_event = await coordinator.process(command)
        assert interrupt_event["type"] == "interrupt.requested"
        with pytest.raises(ScopeCancelledError, match="user_requested"):
            await asyncio.wait_for(model_task, timeout=2)

        await asyncio.wait_for(server.client_closed.wait(), timeout=2)
        assert server.sent_frame_count == target_index + 1
        assert server.server_closed.is_set() is True
    finally:
        if model_task is not None and not model_task.done():
            await scope.cancel("test_cleanup")
            model_task.cancel()
            await asyncio.gather(model_task, return_exceptions=True)
        reset_current_model_delta_sink(sink_token)
        reset_current_turn_execution_scope(scope_token)
        await store.unsubscribe(subscription)
        await scope.close()
        _restore_litellm_client(client, previous_session, previous_factory)
        await client.aclose()
        await server.close()


@pytest.mark.asyncio
async def test_interrupt_during_block_delta_persistence_closes_started_block(
    message_stream_context: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    """delta 尚未线性化时中断，不能留下只有 block.started 的运行块。"""
    _, resolver, session_id = message_stream_context
    store = _BlockDeltaBarrierStore(path_resolver=resolver)
    writer = await store.open(
        session_id=session_id,
        turn_id="job_block_delta_commit_race",
    )
    runtime = MessageStreamRuntime(writer)
    scope = TurnExecutionScope(writer.turn_stream_id)
    delta_task = asyncio.create_task(
        runtime.start_model("model_block_delta_race", "test-provider")
    )
    await asyncio.wait_for(delta_task, timeout=2)
    delta_task = asyncio.create_task(
        runtime.accept_message_chunk(
            AIMessageChunk(
                content=[
                    {
                        "type": "text",
                        "id": "block_race",
                        "index": 0,
                        "text": "尚未落盘",
                    }
                ]
            )
        )
    )
    await asyncio.wait_for(store.delta_commit_started.wait(), timeout=2)

    inbox = AgentControlInbox(writer.turn_stream_id)
    coordinator = AgentLoopControlCoordinator(
        scope=scope,
        inbox=inbox,
        writer=writer,
    )
    command = inbox.accept(
        command_id="interrupt_block_delta_race",
        kind="interrupt",
        idempotency_key="interrupt_block_delta_race",
        payload={"reason": "user_requested"},
    )
    interrupt_event = await coordinator.process(command)
    assert interrupt_event["type"] == "interrupt.requested"

    store.release_delta_commit.set()
    with pytest.raises(MessageStreamTerminalError, match="中断闸门"):
        await asyncio.wait_for(delta_task, timeout=2)
    await runtime.finalize_interruption_facts()
    await writer.close_interrupted(command.command_id)

    events = await store.list_events(
        session_id=session_id,
        turn_stream_id=writer.turn_stream_id,
    )
    event_types = [str(event["type"]) for event in events]
    assert "block.delta" not in event_types
    assert event_types.count("block.started") == 1
    assert event_types.count("block.completed") == 1
    assert event_types[-1] == "stream.interrupted"
    block_completed = next(
        event for event in events if event["type"] == "block.completed"
    )
    assert block_completed["payload"]["partial"] is True
    assert block_completed["payload"]["completion_reason"] == "user_interrupt"

    state = await store.get_state(writer.turn_stream_id)
    assert state["stream_status"] == "interrupted"
    assert all(block["status"] != "running" for block in state["blocks"])
    assert state["blocks"][0]["partial"] is True
    await scope.close()


@pytest.mark.asyncio
async def test_tool_interrupt_reaps_real_subprocess_and_projects_unknown_result(
    message_stream_context: tuple[MessageStreamStore, SessionPathResolver, str],
) -> None:
    """真实工具子进程被打断后必须回收，结果仍投影为未知。"""
    store, resolver, session_id = message_stream_context
    writer = await store.open(
        session_id=session_id,
        turn_id="job_real_subprocess_tool_interrupt",
    )
    runtime = MessageStreamRuntime(writer)
    scope = TurnExecutionScope(writer.turn_stream_id)
    process: asyncio.subprocess.Process | None = None
    process_wait_task: asyncio.Task[int] | None = None
    try:
        await runtime.start_model("model_real_subprocess", "test-provider")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        tool_scope = scope.child("subprocess-tool")

        async def stop_process(_reason: str) -> None:
            if process.returncode is None:
                process.terminate()
                await process.wait()

        tool_scope.register_abort(stop_process)
        await runtime.start_tool(
            tool_execution_id="exec_real_subprocess",
            tool_call_id="call_real_subprocess",
            tool_name="run_process",
        )
        process_wait_task = asyncio.create_task(process.wait())
        await asyncio.sleep(0.05)
        assert process.returncode is None

        inbox = AgentControlInbox(writer.turn_stream_id)
        coordinator = AgentLoopControlCoordinator(
            scope=scope,
            inbox=inbox,
            writer=writer,
        )
        command = inbox.accept(
            command_id="interrupt_real_subprocess",
            kind="interrupt",
            idempotency_key="interrupt_real_subprocess",
            payload={"reason": "user_requested"},
        )
        interrupt_event = await coordinator.process(command)
        assert interrupt_event["type"] == "interrupt.requested"
        await asyncio.wait_for(process_wait_task, timeout=2)
        assert process.returncode is not None

        await runtime.finalize_interruption_facts()
        await writer.close_interrupted(command.command_id)
        events = await store.list_events(
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
        )
        event_types = [str(event["type"]) for event in events]
        assert event_types[-1] == "stream.interrupted"
        assert event_types.index("tool.completed") > event_types.index(
            "interrupt.requested"
        )
        state = await store.get_state(writer.turn_stream_id)
        execution = state["tool_executions"][0]
        assert execution["status"] == "completed"
        assert execution["outcome"] == "outcome_unknown"
        assert execution["completion_reason"] == "provider_failed"

        restarted = MessageStreamStore(path_resolver=resolver)
        recovered_writer = await restarted.open_existing(
            session_id=session_id,
            turn_id=writer.turn_id,
            turn_stream_id=writer.turn_stream_id,
        )
        snapshot = await recovered_writer.snapshot()
        assert snapshot["payload"]["tool_executions"][0]["outcome"] == (
            "outcome_unknown"
        )
        assert snapshot["payload"]["active_state"]["kind"] == "terminal"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if process_wait_task is not None and not process_wait_task.done():
            process_wait_task.cancel()
            await asyncio.gather(process_wait_task, return_exceptions=True)
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chat", "responses"])
async def test_provider_pending_read_crash_recovers_execution_lost(
    message_stream_context: tuple[MessageStreamStore, SessionPathResolver, str],
    provider: ProviderKind,
) -> None:
    """Provider pending read 时进程消失，重启只能投影 execution_lost。"""
    _, resolver, session_id = message_stream_context
    frames = _scenario_frames(provider, "reasoning")
    codec = get_protocol_codec(_protocol(provider))
    wire_frames = tuple(codec.encode(frame) for frame in frames)
    target_index = _target_frame_index(frames, provider=provider, stage="reasoning")
    server = _TCPGatedSSEServer(wire_frames, pause_after=target_index)
    await server.start()
    ready_path = OUTPUT_ROOT / f"crash-worker-{provider}.ready"
    turn_id = f"job_provider_crash_{provider}"
    worker: asyncio.subprocess.Process | None = None
    try:
        worker = await asyncio.create_subprocess_exec(
            *[
                sys.executable,
                "-m",
                "tests.support.model_stream_crash_worker",
                "--provider",
                provider,
                "--endpoint",
                server.endpoint,
                "--sessions-root",
                str(resolver.sessions_root),
                "--session-id",
                session_id,
                "--turn-id",
                turn_id,
                "--ready-path",
                str(ready_path),
            ],
            cwd=Path.cwd(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await _wait_for_worker_ready(ready_path, worker)
        await asyncio.wait_for(server.target_sent.wait(), timeout=2)
        assert worker.returncode is None

        probe = MessageStreamStore(path_resolver=resolver)
        probe_writer = await probe.open_existing(
            session_id=session_id,
            turn_id=turn_id,
        )
        before_crash = await probe.list_events(
            session_id=session_id,
            turn_stream_id=probe_writer.turn_stream_id,
        )
        assert any(event["type"] == "block.delta" for event in before_crash)

        worker.kill()
        await asyncio.wait_for(worker.wait(), timeout=12)
        await asyncio.wait_for(server.client_closed.wait(), timeout=2)
        assert server.sent_frame_count == target_index + 1

        restarted = MessageStreamStore(path_resolver=resolver)
        assert await restarted.reconcile_unfinished_streams() == 1
        recovered_writer = await restarted.open_existing(
            session_id=session_id,
            turn_id=turn_id,
        )
        events = await restarted.list_events(
            session_id=session_id,
            turn_stream_id=recovered_writer.turn_stream_id,
        )
        assert [event["type"] for event in events][-1] == "stream.failed"
        assert "stream.interrupted" not in [event["type"] for event in events]

        state = await restarted.get_state(recovered_writer.turn_stream_id)
        assert state["stream_status"] == "failed"
        assert state["failure"]["code"] == "execution_lost"
        assert state["failure"]["after_interrupt_requested"] is False
        assert state["resumable"] is False
        assert state["recovery"]["status"] == "execution_lost"
        snapshot = await recovered_writer.snapshot()
        assert snapshot["event_seq"] == events[-1]["event_seq"]
        assert snapshot["payload"]["snapshot_seq"] == events[-1]["event_seq"]
        assert snapshot["payload"]["stream_status"] == "failed"
        assert snapshot["payload"]["active_state"]["kind"] == "terminal"
        reconnect_event = await _read_reconnect_terminal_event(
            restarted,
            session_id=session_id,
            turn_stream_id=recovered_writer.turn_stream_id,
            after_seq=int(events[-1]["event_seq"]) - 1,
        )
        assert reconnect_event["type"] == "stream.failed"
        assert reconnect_event["event_seq"] == events[-1]["event_seq"]
    finally:
        if worker is not None and worker.returncode is None:
            worker.kill()
            await worker.wait()
        await server.close()
