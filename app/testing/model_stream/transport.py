from __future__ import annotations

import json
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

import httpx
import litellm
import litellm.llms.custom_httpx.llm_http_handler as litellm_http_handler

from .assets import (
    ModelStreamCassette,
    StreamFrame,
    StreamScenario,
    build_cassette,
    load_scenario,
)
from .config import ModelStreamConfig, load_model_stream_config_from_environment
from .errors import ModelStreamAssetError, ModelStreamError
from .matcher import redact_url_for_asset, safe_request_match_fields
from .protocols import StreamProtocolCodec, get_protocol_codec
from .replay import ReplayCoordinator


class _ReplayByteStream(httpx.AsyncByteStream):
    def __init__(
        self,
        frames: tuple[StreamFrame, ...],
        codec: StreamProtocolCodec,
    ) -> None:
        self._frames = frames
        self._codec = codec
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            if self._closed:
                return
            yield self._codec.encode(frame)

    async def aclose(self) -> None:
        self._closed = True


class _SSEFrameRecorder:
    def __init__(self, codec: StreamProtocolCodec) -> None:
        self._buffer = bytearray()
        self._codec = codec
        self.frames: list[StreamFrame] = []
        self.saw_done = False

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            end = self._frame_end()
            if end is None:
                return
            raw_frame = bytes(self._buffer[:end])
            del self._buffer[: end + self._delimiter_length(end)]
            self._parse_frame(raw_frame)

    def finish(self) -> None:
        if self._buffer:
            raise ModelStreamAssetError(
                "recorded response 结束时存在未完成的 SSE frame"
            )
        if not self.saw_done:
            raise ModelStreamAssetError(
                f"recorded response 缺少 protocol={self._codec.protocol_id!r} terminal frame"
            )
        self._codec.validate_stream(
            self.frames,
            label=f"recorded response protocol={self._codec.protocol_id!r}",
        )

    def _frame_end(self) -> int | None:
        lf_end = bytes(self._buffer).find(b"\n\n")
        crlf_end = bytes(self._buffer).find(b"\r\n\r\n")
        positions = [position for position in (lf_end, crlf_end) if position >= 0]
        return min(positions) if positions else None

    def _delimiter_length(self, end: int) -> int:
        return 4 if bytes(self._buffer[end : end + 4]) == b"\r\n\r\n" else 2

    def _parse_frame(self, raw_frame: bytes) -> None:
        text = raw_frame.decode("utf-8")
        data_lines: list[str] = []
        event_name: str | None = None
        for line in text.replace("\r\n", "\n").split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].removeprefix(" ")
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
        if not data_lines:
            return
        data = "\n".join(data_lines)
        frame = self._codec.decode(event_name=event_name, data=data)
        self.frames.append(frame)
        if self._codec.is_terminal(frame):
            self.saw_done = True


class _RecordingByteStream(httpx.AsyncByteStream):
    def __init__(
        self,
        upstream: httpx.AsyncByteStream,
        recorder: _SSEFrameRecorder,
        complete: Callable[[], None],
        incomplete: Callable[[BaseException | None], None],
    ) -> None:
        self._upstream = upstream
        self._recorder = recorder
        self._complete = complete
        self._incomplete = incomplete
        self._finished = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._upstream:
                self._recorder.feed(chunk)
                yield chunk
            self._recorder.finish()
            self._complete()
            self._finished = True
        except BaseException as error:
            self._incomplete(error)
            self._finished = True
            raise

    async def aclose(self) -> None:
        if not self._finished:
            self._incomplete(None)
            self._finished = True
        await self._upstream.aclose()


def _request_body(request: httpx.Request) -> dict[str, object]:
    try:
        parsed: object = json.loads(request.content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    safe_names = {"content-type", "cache-control", "x-request-id"}
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in safe_names
    }


def _recorded_cassette(
    *,
    request: httpx.Request,
    response: httpx.Response,
    frames: tuple[StreamFrame, ...],
    asset_id: str,
    protocol: str,
) -> ModelStreamCassette:
    body = _request_body(request)
    model = body.get("model")
    match = safe_request_match_fields(request)
    if "stream" not in match:
        match["stream"] = body.get("stream") is True
    if isinstance(model, str) and "model" not in match:
        match["model"] = model
    cassette = build_cassette(
        asset_id=asset_id,
        url=redact_url_for_asset(str(request.url)),
        model=str(model or "recorded-model"),
        match={key: value for key, value in match.items() if key != "model"},
        frames=frames,
        protocol=protocol,
        source="recorded",
        status=response.status_code,
        headers=_safe_response_headers(response.headers),
    )
    return cassette


def _write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(encoded)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


class ModelStreamHTTPTransport(httpx.AsyncBaseTransport):
    """LiteLLM 使用的 HTTPX transport：replay 不触网，record 旁路采集。"""

    def __init__(
        self,
        *,
        scenario: StreamScenario,
        mode: str,
        replay_policy: str,
        artifact_root: Path,
        matching_policy: str = "strict",
        live_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if mode not in {"record", "replay"}:
            raise ModelStreamError(f"不支持的 model stream transport mode: {mode!r}")
        if matching_policy != "strict":
            raise ModelStreamError(
                f"不支持的 model stream matching policy: {matching_policy!r}"
            )
        if replay_policy not in {"request_reusable", "session_sequence"}:
            raise ModelStreamError(f"不支持的 replay policy: {replay_policy!r}")
        protocol = scenario.cassette.metadata.get("protocol")
        if not isinstance(protocol, str):
            raise ModelStreamError("model stream cassette 缺少有效 protocol metadata")
        codec = None if protocol == "mixed" else get_protocol_codec(protocol)
        if codec is not None:
            codec.require_runtime()
        elif mode == "record":
            raise ModelStreamError(
                "model stream mixed cassette 仅支持 replay；record 必须为单一 protocol"
            )
        self._scenario = scenario
        self._codec = codec
        self._mode = mode
        self._artifact_root = artifact_root
        self._coordinator = ReplayCoordinator(
            cassette=scenario.cassette,
            scenario_id=scenario.scenario_id,
            policy=replay_policy,  # type: ignore[arg-type]
        )
        self._live_transport = (
            live_transport if live_transport is not None else httpx.AsyncHTTPTransport()
        ) if mode == "record" else None
        self._call_count = 0
        self._request_urls: list[str] = []
        self._incomplete_artifacts: list[Path] = []

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def request_urls(self) -> tuple[str, ...]:
        return tuple(self._request_urls)

    @property
    def hit_counts(self) -> dict[int, int]:
        return self._coordinator.hit_counts()

    @property
    def incomplete_artifacts(self) -> tuple[Path, ...]:
        return tuple(self._incomplete_artifacts)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._call_count += 1
        self._request_urls.append(str(request.url))
        if self._mode == "replay":
            interaction = self._coordinator.select(request)
            codec = get_protocol_codec(interaction.protocol)
            codec.require_runtime()
            return httpx.Response(
                status_code=interaction.response.status,
                headers=interaction.response.headers,
                stream=_ReplayByteStream(interaction.response.frames, codec),
                request=request,
            )
        if self._live_transport is None:
            raise RuntimeError("record transport 缺少 live HTTP transport")
        if self._codec is None:
            raise RuntimeError("record transport 缺少单一 protocol codec")
        upstream = await self._live_transport.handle_async_request(request)
        recorder = _SSEFrameRecorder(self._codec)
        asset_id = str(self._scenario.cassette.metadata["asset_id"])

        def complete() -> None:
            cassette = _recorded_cassette(
                request=request,
                response=upstream,
                frames=tuple(recorder.frames),
                asset_id=asset_id,
                protocol=self._codec.protocol_id,
            )
            output_path = self._artifact_root / f"{asset_id}-{time.time_ns()}.json"
            _write_json_atomically(output_path, cassette.raw)

        def incomplete(error: BaseException | None) -> None:
            del error
            output_path = self._artifact_root / f"{asset_id}-{time.time_ns()}.incomplete.json"
            diagnostic: dict[str, object] = {
                "schema_version": 1,
                "kind": "model_stream_incomplete",
                "scenario_id": self._scenario.scenario_id,
                "asset_id": asset_id,
                "frame_count": len(recorder.frames),
                "saw_done": recorder.saw_done,
            }
            _write_json_atomically(output_path, diagnostic)
            self._incomplete_artifacts.append(output_path)

        stream = _RecordingByteStream(
            upstream.stream,
            recorder,
            complete,
            incomplete,
        )
        return httpx.Response(
            status_code=upstream.status_code,
            headers=upstream.headers,
            stream=stream,
            request=request,
            extensions=upstream.extensions,
        )

    async def aclose(self) -> None:
        if self._live_transport is not None:
            await self._live_transport.aclose()


class ModelStreamTransportController:
    def __init__(
        self,
        *,
        config: ModelStreamConfig,
        scenario: StreamScenario,
        client: httpx.AsyncClient,
        transport: ModelStreamHTTPTransport,
        previous_session: httpx.AsyncClient | None,
        previous_async_client_factory: object,
    ) -> None:
        self.config = config
        self.scenario = scenario
        self.client = client
        self.transport = transport
        self._previous_session = previous_session
        self._previous_async_client_factory = previous_async_client_factory
        self._closed = False

    @classmethod
    def install(cls, config: ModelStreamConfig) -> ModelStreamTransportController | None:
        if config.transport.mode == "off":
            return None
        if config.transport.fixture_root is None or config.transport.scenario_id is None:
            raise ModelStreamError("启用 model stream transport 时缺少 fixture_root 或 scenario_id")
        scenario = load_scenario(
            config.transport.fixture_root,
            config.transport.scenario_id,
        )
        previous_session = litellm.aclient_session
        if previous_session is not None:
            raise ModelStreamError(
                "无法安装 model stream transport：LiteLLM async client 已经存在，"
                "必须在第一次模型请求前安装"
            )
        transport = ModelStreamHTTPTransport(
            scenario=scenario,
            mode=config.transport.mode,
            matching_policy=config.transport.matching_policy,
            replay_policy=config.transport.replay_policy,
            artifact_root=config.transport.artifact_root,
        )
        client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
        )
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        injected_handler = AsyncHTTPHandler.__new__(AsyncHTTPHandler)
        injected_handler.timeout = None
        injected_handler.event_hooks = None
        injected_handler.client_alias = "boxteam-model-stream"
        injected_handler.client = client
        previous_async_client_factory = litellm_http_handler.get_async_httpx_client

        # TODO: LiteLLM 尚未给 Responses API 暴露稳定的 AsyncClient 注入参数；
        # 待上游提供正式扩展点后，删除这个进程内 factory hook。
        def get_injected_async_client(*_args: object, **_kwargs: object) -> AsyncHTTPHandler:
            return injected_handler

        litellm_http_handler.get_async_httpx_client = get_injected_async_client
        litellm.aclient_session = client
        return cls(
            config=config,
            scenario=scenario,
            client=client,
            transport=transport,
            previous_session=previous_session,
            previous_async_client_factory=previous_async_client_factory,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        litellm_http_handler.get_async_httpx_client = self._previous_async_client_factory
        if litellm.aclient_session is self.client:
            litellm.aclient_session = self._previous_session
        await self.client.aclose()


def install_model_stream_from_environment(
    *,
    project_root: Path | str | None = None,
) -> ModelStreamTransportController | None:
    config = load_model_stream_config_from_environment(project_root=project_root)
    if config is None:
        return None
    return ModelStreamTransportController.install(config)
