from __future__ import annotations

import json
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

import httpx

from app.core.atomic_fs import native_io_path
from app.core.phase_observer import observe_phase

from .assets import (
    ModelStreamCassette,
    StreamFrame,
    StreamScenario,
    build_cassette,
)
from .context import current_model_call_identity
from .errors import ModelStreamAssetError, ModelStreamError
from .matcher import redact_url_for_asset, safe_request_match_fields
from .protocols import StreamProtocolCodec, get_protocol_codec
from .replay import ReplayCoordinator


class _ReplayByteStream(httpx.AsyncByteStream):
    def __init__(self, frames: tuple[StreamFrame, ...], codec: StreamProtocolCodec) -> None:
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
    path = native_io_path(path)
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
        if replay_policy not in {"request_reusable", "call_identity"}:
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
        self._replay_policy = replay_policy

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
            if self._replay_policy == "call_identity":
                identity = current_model_call_identity()
                _write_json_atomically(self._artifact_root / f"{identity.model_call_id}.json", {
                    "identity": identity.to_dict(), "interaction_index": interaction.index,
                    "scenario_id": self._scenario.scenario_id,
                    "request": _request_body(request),
                })
                await observe_phase(identity.model_call_id, "model.response_ready")
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
