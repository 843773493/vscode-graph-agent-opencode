"""联邦 peer RPC channel 的帧合同与显式错误映射。

channel 建立后，双方在同一 WebSocket 上双向发起 request/response/event，因此
帧必须自带 correlation 与显式类型；畸形帧、未知方法、未知 correlation 都
响亮失败，不做静默忽略，也不返回虚假默认值。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Literal

from app.gateway.federation.errors import (
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_UNKNOWN_METHOD,
    FederationError,
)

FrameType = Literal["hello", "welcome", "ping", "pong", "request", "response"]

FRAME_HELLO = "hello"
FRAME_WELCOME = "welcome"
FRAME_PING = "ping"
FRAME_PONG = "pong"
FRAME_REQUEST = "request"
FRAME_RESPONSE = "response"

# 方法名是跨 Gateway 的稳定合同；新增方法必须显式登记。
METHOD_DISCOVERY = "federation.discovery"
METHOD_RELAY = "federation.relay"
METHOD_STATUS = "federation.status"
KNOWN_METHODS = frozenset({METHOD_DISCOVERY, METHOD_RELAY, METHOD_STATUS})


def new_correlation_id() -> str:
    return f"cor_{secrets.token_hex(12)}"


@dataclass(frozen=True, slots=True)
class FederationFrame:
    """一个已解码的 channel 帧；``payload`` 随 ``frame_type`` 解释。"""

    frame_type: str
    payload: dict[str, object]

    def encode(self) -> str:
        return json.dumps(
            {"frame_type": self.frame_type, "payload": self.payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, raw: str) -> FederationFrame:
        if not isinstance(raw, str):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "帧必须是文本消息")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise FederationError(
                FEDERATION_MALFORMED_FRAME, f"帧不是合法 JSON: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "帧必须是 JSON 对象")
        frame_type = parsed.get("frame_type")
        if not isinstance(frame_type, str) or not frame_type:
            raise FederationError(
                FEDERATION_MALFORMED_FRAME, "帧缺少 frame_type"
            )
        if frame_type not in (
            FRAME_HELLO,
            FRAME_WELCOME,
            FRAME_PING,
            FRAME_PONG,
            FRAME_REQUEST,
            FRAME_RESPONSE,
        ):
            raise FederationError(
                FEDERATION_MALFORMED_FRAME, f"未知 frame_type: {frame_type!r}"
            )
        payload = parsed.get("payload")
        if not isinstance(payload, dict):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "帧缺少 payload 对象")
        return cls(frame_type=frame_type, payload=payload)


def build_request_frame(
    *,
    correlation_id: str,
    method: str,
    request: dict[str, object],
    deadline_at: float | None = None,
) -> FederationFrame:
    if method not in KNOWN_METHODS:
        raise FederationError(FEDERATION_UNKNOWN_METHOD, f"未知联邦方法: {method!r}")
    payload: dict[str, object] = {
        "correlation_id": correlation_id,
        "method": method,
        "request": request,
    }
    if deadline_at is not None:
        payload["deadline_at"] = deadline_at
    return FederationFrame(frame_type=FRAME_REQUEST, payload=payload)


def build_response_frame(
    *,
    correlation_id: str,
    result: dict[str, object] | None = None,
    error: FederationError | None = None,
) -> FederationFrame:
    if (result is None) == (error is None):
        raise ValueError("response 帧必须恰好包含 result 或 error 之一")
    payload: dict[str, object] = {"correlation_id": correlation_id}
    if error is not None:
        payload["ok"] = False
        payload["error"] = error.to_wire()
    else:
        payload["ok"] = True
        payload["result"] = result
    return FederationFrame(frame_type=FRAME_RESPONSE, payload=payload)


def require_str(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, f"帧字段 {field} 必须是非空字符串"
        )
    return value


def optional_float(payload: dict[str, object], field: str) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, f"帧字段 {field} 必须是数字"
        )
    return float(value)


def require_mapping(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, f"帧字段 {field} 必须是对象"
        )
    return value


def require_correlation_id(payload: dict[str, object]) -> str:
    return require_str(payload, "correlation_id")


__all__ = [
    "FRAME_HELLO",
    "FRAME_PING",
    "FRAME_PONG",
    "FRAME_REQUEST",
    "FRAME_RESPONSE",
    "FRAME_WELCOME",
    "KNOWN_METHODS",
    "METHOD_DISCOVERY",
    "METHOD_RELAY",
    "METHOD_STATUS",
    "FederationFrame",
    "FrameType",
    "build_request_frame",
    "build_response_frame",
    "new_correlation_id",
    "optional_float",
    "require_correlation_id",
    "require_mapping",
    "require_str",
]
