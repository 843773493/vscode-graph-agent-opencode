"""联邦 channel/grant 的显式错误类型。

本地控制面必须响亮失败：任何鉴权、完整性、audience/path、防重放、capability
或 transit 上限问题都必须抛出携带稳定错误码的异常，调用方据此返回可诊断的
错误帧，绝不静默降级或返回虚假默认值。
"""

from __future__ import annotations


class FederationError(Exception):
    """联邦协议错误：``code`` 是稳定合同，``detail`` 供诊断。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, object] | None = None,
    ) -> None:
        if not code or not message:
            raise ValueError("FederationError 需要非空 code 与 message")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = dict(detail or {})

    def to_wire(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


# 稳定错误码闭集；新增错误必须显式登记，避免调用方猜测字符串。
FEDERATION_MISSING_CREDENTIAL = "federation-missing-credential"
FEDERATION_INVALID_CREDENTIAL = "federation-invalid-credential"
FEDERATION_UNKNOWN_PEER = "federation-unknown-peer"
FEDERATION_MALFORMED_FRAME = "federation-malformed-frame"
FEDERATION_UNKNOWN_METHOD = "federation-unknown-method"
FEDERATION_CHANNEL_CLOSED = "federation-channel-closed"
FEDERATION_CHANNEL_HEARTBEAT_TIMEOUT = "federation-channel-heartbeat-timeout"
FEDERATION_CHANNEL_EPOCH_STALE = "federation-channel-epoch-stale"
FEDERATION_GRANT_INVALID_SIGNATURE = "federation-grant-invalid-signature"
FEDERATION_GRANT_EXPIRED = "federation-grant-expired"
FEDERATION_GRANT_AUDIENCE_MISMATCH = "federation-grant-audience-mismatch"
FEDERATION_GRANT_PATH_MISMATCH = "federation-grant-path-mismatch"
FEDERATION_GRANT_ORIGIN_MISMATCH = "federation-grant-origin-mismatch"
FEDERATION_GRANT_KIND_MISMATCH = "federation-grant-kind-mismatch"
FEDERATION_GRANT_LIFETIME_INSUFFICIENT = "federation-grant-lifetime-insufficient"
FEDERATION_GRANT_REPLAY = "federation-grant-replay"
FEDERATION_GRANT_REPLAY_CONFLICT = "federation-grant-replay-conflict"
FEDERATION_TRANSIT_LIMIT_EXCEEDED = "federation-transit-limit-exceeded"
FEDERATION_DEADLINE_EXCEEDED = "federation-deadline-exceeded"
FEDERATION_AUTHORIZATION_REVOKED = "authorization-revoked"
FEDERATION_HARDENING_ACTIVE = "federation-hardening-active"
FEDERATION_TARGET_NOT_RESOLVABLE = "target_not_resolvable"
FEDERATION_TARGET_AMBIGUOUS = "target_ambiguous"
FEDERATION_CAPABILITY_DENIED = "federation-capability-denied"
FEDERATION_SESSION_MAIN_UNAVAILABLE = "federation-session-main-unavailable"

# WebSocket close code 合同：协议错误 1002、鉴权/策略 1008、内部失败 1011。
CLOSE_CODE_PROTOCOL_ERROR = 1002
CLOSE_CODE_POLICY_VIOLATION = 1008
CLOSE_CODE_INTERNAL_ERROR = 1011

_PROTOCOL_ERROR_CODES = frozenset(
    {
        FEDERATION_MALFORMED_FRAME,
        FEDERATION_UNKNOWN_METHOD,
    }
)
_POLICY_ERROR_CODES = frozenset(
    {
        FEDERATION_MISSING_CREDENTIAL,
        FEDERATION_INVALID_CREDENTIAL,
        FEDERATION_UNKNOWN_PEER,
        FEDERATION_CHANNEL_EPOCH_STALE,
        FEDERATION_GRANT_INVALID_SIGNATURE,
        FEDERATION_GRANT_EXPIRED,
        FEDERATION_GRANT_AUDIENCE_MISMATCH,
        FEDERATION_GRANT_PATH_MISMATCH,
        FEDERATION_GRANT_ORIGIN_MISMATCH,
        FEDERATION_GRANT_KIND_MISMATCH,
        FEDERATION_GRANT_LIFETIME_INSUFFICIENT,
        FEDERATION_GRANT_REPLAY,
        FEDERATION_GRANT_REPLAY_CONFLICT,
        FEDERATION_TRANSIT_LIMIT_EXCEEDED,
        FEDERATION_AUTHORIZATION_REVOKED,
        FEDERATION_HARDENING_ACTIVE,
    }
)


def close_code_for(error: FederationError) -> int:
    """把联邦错误映射为稳定的 WebSocket close code。"""

    if error.code in _PROTOCOL_ERROR_CODES:
        return CLOSE_CODE_PROTOCOL_ERROR
    if error.code in _POLICY_ERROR_CODES:
        return CLOSE_CODE_POLICY_VIOLATION
    return CLOSE_CODE_INTERNAL_ERROR


__all__ = [
    "CLOSE_CODE_INTERNAL_ERROR",
    "CLOSE_CODE_POLICY_VIOLATION",
    "CLOSE_CODE_PROTOCOL_ERROR",
    "FEDERATION_AUTHORIZATION_REVOKED",
    "FEDERATION_CAPABILITY_DENIED",
    "FEDERATION_CHANNEL_CLOSED",
    "FEDERATION_CHANNEL_EPOCH_STALE",
    "FEDERATION_CHANNEL_HEARTBEAT_TIMEOUT",
    "FEDERATION_DEADLINE_EXCEEDED",
    "FEDERATION_GRANT_AUDIENCE_MISMATCH",
    "FEDERATION_GRANT_EXPIRED",
    "FEDERATION_GRANT_INVALID_SIGNATURE",
    "FEDERATION_GRANT_KIND_MISMATCH",
    "FEDERATION_GRANT_LIFETIME_INSUFFICIENT",
    "FEDERATION_GRANT_ORIGIN_MISMATCH",
    "FEDERATION_GRANT_PATH_MISMATCH",
    "FEDERATION_GRANT_REPLAY",
    "FEDERATION_GRANT_REPLAY_CONFLICT",
    "FEDERATION_HARDENING_ACTIVE",
    "FEDERATION_INVALID_CREDENTIAL",
    "FEDERATION_MALFORMED_FRAME",
    "FEDERATION_MISSING_CREDENTIAL",
    "FEDERATION_SESSION_MAIN_UNAVAILABLE",
    "FEDERATION_TARGET_AMBIGUOUS",
    "FEDERATION_TARGET_NOT_RESOLVABLE",
    "FEDERATION_TRANSIT_LIMIT_EXCEEDED",
    "FEDERATION_UNKNOWN_METHOD",
    "FEDERATION_UNKNOWN_PEER",
    "FederationError",
    "close_code_for",
]
