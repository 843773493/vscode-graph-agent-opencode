from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import cast

from .assets import ProtocolId, StreamFrame
from .errors import ModelStreamAssetError, ModelStreamProtocolError


class StreamProtocolCodec(ABC):
    """一个 provider SSE 协议的 codec contract。"""

    protocol_id: ProtocolId
    runtime_supported: bool = True

    def require_runtime(self) -> None:
        if not self.runtime_supported:
            raise ModelStreamProtocolError(
                f"model stream protocol {self.protocol_id!r} codec 暂未实现，"
                "缺少真实 provider 资源和运行时处理"
            )

    @abstractmethod
    def decode(self, *, event_name: str | None, data: str) -> StreamFrame:
        """将一个已经按 SSE frame 聚合的 event 解码为 Provider frame。"""

    @abstractmethod
    def encode(self, frame: StreamFrame) -> bytes:
        """将 Provider frame 编码为下游 HTTP client 可读取的 SSE bytes。"""

    @abstractmethod
    def is_terminal(self, frame: StreamFrame) -> bool:
        """判断 frame 是否为该协议的终止事件。"""

    @abstractmethod
    def validate_frame(self, frame: StreamFrame, *, label: str) -> None:
        """校验协议专用的单 frame 约束。"""

    def validate_stream(self, frames: Sequence[StreamFrame], *, label: str) -> None:
        if not frames:
            raise ModelStreamProtocolError(
                f"{label} protocol={self.protocol_id!r} 不能是空 stream"
            )
        terminal_indexes: list[int] = []
        for index, frame in enumerate(frames):
            frame_label = f"{label}.frames[{index}]"
            self.validate_frame(frame, label=frame_label)
            if self.is_terminal(frame):
                terminal_indexes.append(index)
        if terminal_indexes != [len(frames) - 1]:
            raise ModelStreamProtocolError(
                f"{label} protocol={self.protocol_id!r} 必须只有一个位于末尾的 terminal frame，"
                f"actual={terminal_indexes!r}"
            )


def _encode_sse(*, event_name: str | None, data: str) -> bytes:
    lines: list[str] = []
    if event_name is not None:
        lines.append(f"event: {event_name}")
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _json_payload(frame: StreamFrame, *, label: str) -> str:
    if frame.encoding != "json" or not isinstance(frame.payload, dict):
        raise ModelStreamProtocolError(
            f"{label} 必须是 encoding=json 且 payload 为 object"
        )
    return json.dumps(
        frame.payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class OpenAIChatCompletionsCodec(StreamProtocolCodec):
    protocol_id: ProtocolId = "openai_chat_sse"

    def decode(self, *, event_name: str | None, data: str) -> StreamFrame:
        if data == "[DONE]":
            return StreamFrame(
                kind="done",
                encoding="text",
                payload="[DONE]",
                event=event_name,
            )
        try:
            parsed: object = json.loads(data)
        except json.JSONDecodeError:
            return StreamFrame(
                kind="data",
                encoding="text",
                payload=data,
                event=event_name,
            )
        if isinstance(parsed, dict):
            return StreamFrame(
                kind="data",
                encoding="json",
                payload=parsed,
                event=event_name,
            )
        return StreamFrame(
            kind="data",
            encoding="text",
            payload=data,
            event=event_name,
        )

    def encode(self, frame: StreamFrame) -> bytes:
        self.validate_frame(frame, label="replay frame")
        if frame.kind == "done":
            return _encode_sse(event_name=frame.event, data="[DONE]")
        if frame.encoding == "text":
            if not isinstance(frame.payload, str):
                raise ModelStreamProtocolError("Chat text frame payload 必须是字符串")
            return _encode_sse(event_name=frame.event, data=frame.payload)
        return _encode_sse(
            event_name=frame.event,
            data=_json_payload(frame, label="Chat JSON frame"),
        )

    def is_terminal(self, frame: StreamFrame) -> bool:
        return (
            frame.kind == "done"
            and frame.encoding == "text"
            and frame.payload == "[DONE]"
        )

    def validate_frame(self, frame: StreamFrame, *, label: str) -> None:
        if frame.kind == "done":
            if not self.is_terminal(frame):
                raise ModelStreamProtocolError(
                    f"{label} Chat terminal 必须是 encoding=text、payload='[DONE]'"
                )
            return
        if frame.encoding == "json" and not isinstance(frame.payload, dict):
            raise ModelStreamProtocolError(
                f"{label} Chat JSON data payload 必须是 object"
            )
        if frame.encoding == "text" and not isinstance(frame.payload, str):
            raise ModelStreamProtocolError(
                f"{label} Chat text data payload 必须是字符串"
            )


class OpenAIResponsesCodec(StreamProtocolCodec):
    protocol_id: ProtocolId = "openai_responses_sse"

    def decode(self, *, event_name: str | None, data: str) -> StreamFrame:
        try:
            parsed: object = json.loads(data)
        except json.JSONDecodeError as error:
            raise ModelStreamProtocolError(
                "OpenAI Responses SSE data 必须是 JSON object"
            ) from error
        if not isinstance(parsed, dict):
            raise ModelStreamProtocolError(
                "OpenAI Responses SSE data 必须是 JSON object"
            )
        payload_type = parsed.get("type")
        effective_event = (
            event_name
            if event_name is not None
            else payload_type
            if isinstance(payload_type, str)
            else None
        )
        if effective_event is None:
            raise ModelStreamProtocolError(
                "OpenAI Responses SSE event 缺少 event 行和 payload.type"
            )
        return StreamFrame(
            kind=(
                "done"
                if effective_event == "response.completed"
                else "data"
            ),
            encoding="json",
            payload=parsed,
            event=effective_event,
        )

    def encode(self, frame: StreamFrame) -> bytes:
        self.validate_frame(frame, label="replay frame")
        return _encode_sse(
            event_name=frame.event,
            data=_json_payload(frame, label="Responses frame"),
        )

    def is_terminal(self, frame: StreamFrame) -> bool:
        return (
            frame.kind == "done"
            and frame.encoding == "json"
            and frame.event == "response.completed"
            and isinstance(frame.payload, dict)
            and frame.payload.get("type") == "response.completed"
        )

    def validate_frame(self, frame: StreamFrame, *, label: str) -> None:
        payload = frame.payload
        if frame.encoding != "json" or not isinstance(payload, dict):
            raise ModelStreamProtocolError(
                f"{label} Responses frame 必须是 encoding=json、payload object"
            )
        payload_type = payload.get("type")
        if not isinstance(payload_type, str) or not payload_type:
            raise ModelStreamProtocolError(
                f"{label} Responses payload.type 必须是非空字符串"
            )
        if frame.event != payload_type:
            raise ModelStreamProtocolError(
                f"{label} Responses event 与 payload.type 不一致: "
                f"event={frame.event!r} type={payload_type!r}"
            )
        if frame.kind == "done" and not self.is_terminal(frame):
            raise ModelStreamProtocolError(
                f"{label} Responses terminal 必须是 response.completed event"
            )
        if frame.kind == "data" and payload_type == "response.completed":
            raise ModelStreamProtocolError(
                f"{label} response.completed 必须标记为 done frame"
            )


class AnthropicMessagesCodec(StreamProtocolCodec):
    protocol_id: ProtocolId = "anthropic_messages_sse"
    runtime_supported = False

    def _unsupported(self) -> None:
        self.require_runtime()

    def decode(self, *, event_name: str | None, data: str) -> StreamFrame:
        del event_name, data
        self._unsupported()
        raise AssertionError("unreachable")

    def encode(self, frame: StreamFrame) -> bytes:
        del frame
        self._unsupported()
        raise AssertionError("unreachable")

    def is_terminal(self, frame: StreamFrame) -> bool:
        del frame
        self._unsupported()
        raise AssertionError("unreachable")

    def validate_frame(self, frame: StreamFrame, *, label: str) -> None:
        del frame, label
        self._unsupported()


_CODECS: dict[ProtocolId, StreamProtocolCodec] = {
    "openai_chat_sse": OpenAIChatCompletionsCodec(),
    "openai_responses_sse": OpenAIResponsesCodec(),
    "anthropic_messages_sse": AnthropicMessagesCodec(),
}


def get_protocol_codec(protocol: str) -> StreamProtocolCodec:
    codec = _CODECS.get(cast(ProtocolId, protocol))
    if codec is None:
        raise ModelStreamAssetError(
            f"不支持的 model stream protocol: {protocol!r}"
        )
    return codec
