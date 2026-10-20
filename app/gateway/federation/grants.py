"""hub 签发的 discovery/operation grant 与其校验。

跨 Gateway 操作使用 channel-bound origin envelope 与 hub 签发的
``HubTransitDiscoveryGrant``/``HubTransitOperationGrant``，而不是把用户
credential 或 Session link 当授权。grant 绑定 issuer、origin、audience、
transit path、capability、规范 target、request/nonce、deadline，目标以受信
hub 公钥验证签名与 audience/path/期限。

身份、完整性、audience/path 与防重放属于不可关闭的协议正确性：任何一项
不满足都抛出稳定错误码，绝不降级放行。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.gateway.federation.errors import (
    FEDERATION_GRANT_AUDIENCE_MISMATCH,
    FEDERATION_GRANT_EXPIRED,
    FEDERATION_GRANT_INVALID_SIGNATURE,
    FEDERATION_GRANT_KIND_MISMATCH,
    FEDERATION_GRANT_LIFETIME_INSUFFICIENT,
    FEDERATION_GRANT_ORIGIN_MISMATCH,
    FEDERATION_GRANT_PATH_MISMATCH,
    FEDERATION_MALFORMED_FRAME,
    FederationError,
)
from app.gateway.federation.identity import FederationPeerIdentity

GrantKind = Literal["discovery", "operation"]
OperationKind = Literal["send", "read", "wait", "reply", "fork_source", "fork_target"]

# clock skew 与传输重放余量：wait grant 的剩余寿命必须覆盖它加 effective timeout。
GRANT_CLOCK_SKEW_SECONDS = 5.0
DISCOVERY_GRANT_LIFETIME_SECONDS = 10.0
# 只允许一次 hub transit：spoke → hub → spoke 恰为 2 跳。
MAX_TRANSIT_GATEWAYS = 1
MAX_GATEWAY_HOPS = 2


def _b64url_encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as error:  # 畸形编码统一为协议错误
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, "grant 不是合法 base64url"
        ) from error


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class GrantTarget:
    """规范 target：gateway/workspace-qualified session，不含 connection/channel locator。

    ``gateway_id`` 是稳定身份（不是连接、channel 或 route）；它是 operation
    grant 的 audience 判定与 hub transit 转发的路由依据。
    """

    gateway_id: str
    workspace_id: str
    session_id: str

    def to_payload(self) -> dict[str, object]:
        return {
            "gateway_id": self.gateway_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
        }


@dataclass(frozen=True, slots=True)
class ContextGrantTarget:
    """read grant 的真实上下文资源目标，和 Session 通信地址保持各自字段闭集。"""

    gateway_id: str
    resource_identity: str
    uri: str
    scope: str

    def __post_init__(self) -> None:
        from app.services.business.session_context_resource import (
            gateway_session_inventory_identity,
            parse_session_context_ref,
            session_resource_identity,
            workspace_session_inventory_identity,
        )
        resource = parse_session_context_ref(
            resource_identity=self.resource_identity, uri=self.uri, scope=self.scope,
            local_gateway_id=self.gateway_id,
        )
        if resource.gateway_authority is not None:
            raise ValueError("资源 URI authority 与 grant audience 不一致")
        if resource.scope == "workspace":
            if resource.kind == "session" and resource.session_id is not None:
                expected = session_resource_identity(resource.scope_id, resource.session_id)
            elif resource.kind == "workspace_sessions":
                expected = workspace_session_inventory_identity(resource.scope_id)
            else:
                raise ValueError("read grant 只支持现有 Session 上下文资源")
        elif resource.scope == "gateway" and resource.kind == "workspace_sessions" and resource.scope_id == self.gateway_id:
            expected = gateway_session_inventory_identity(self.gateway_id)
        else:
            raise ValueError("read grant 资源 scope 与实际 Gateway 不一致")
        if resource.resource_identity != expected:
            raise ValueError("read grant ResourceIdentity 与 URI owner 不一致")

    def to_payload(self) -> dict[str, object]:
        return {
            "gateway_id": self.gateway_id, "resource_identity": self.resource_identity,
            "uri": self.uri, "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class ResourceNavigationGrantTarget:
    """轻量只读导航绑定固定 URI，不为普通文件访问制造持久身份。"""

    gateway_id: str
    uri: str

    def __post_init__(self) -> None:
        from app.core.resource_uri import parse_resource_uri

        resource = parse_resource_uri(self.uri, local_gateway_id=self.gateway_id)
        if resource.gateway_authority is not None or resource.scope not in {"workspace", "gateway"}:
            raise ValueError("导航 URI 必须属于固定 audience 的 Workspace 或 Gateway")
        if resource.scope == "gateway" and resource.scope_id != self.gateway_id:
            raise ValueError("导航 URI 的 Gateway scope 与 audience 不一致")

    def to_payload(self) -> dict[str, object]:
        return {"gateway_id": self.gateway_id, "uri": self.uri}


@dataclass(frozen=True, slots=True)
class ForkGrantTarget:
    """fork capability 的精确领域 URI；来源导出和目标创建分别授权。"""

    gateway_id: str
    uri: str
    fork_role: Literal["source", "target"]

    def __post_init__(self) -> None:
        from app.core.resource_uri import parse_resource_uri, parse_session_thread_uri

        if self.fork_role not in {"source", "target"}:
            raise ValueError("fork grant role 必须是 source 或 target")
        resource = parse_resource_uri(self.uri, local_gateway_id=self.gateway_id)
        if resource.gateway_authority is not None or resource.scope != "workspace":
            raise ValueError("fork grant URI 不属于固定 audience Workspace")
        if self.fork_role == "source":
            parse_session_thread_uri(self.uri, local_gateway_id=self.gateway_id)
        elif resource.kind not in {"session", "folder"}:
            raise ValueError("fork target grant 必须指向 Session 集合、Session 或 Folder 容器")

    def to_payload(self) -> dict[str, object]:
        return {"gateway_id": self.gateway_id, "uri": self.uri, "fork_role": self.fork_role}


def decode_grant_target(raw: object) -> GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget:
    """grant 和实际 RPC 请求共用同一闭集 decoder，不猜测或补齐目标字段。"""
    if not isinstance(raw, dict):
        raise FederationError(FEDERATION_MALFORMED_FRAME, "grant target 必须是对象")
    if set(raw) == {"gateway_id", "workspace_id", "session_id"}:
        return GrantTarget(**{key: _require_str(raw, key) for key in raw})
    if set(raw) == {"gateway_id", "uri"}:
        try:
            return ResourceNavigationGrantTarget(**{key: _require_str(raw, key) for key in raw})
        except ValueError as error:
            raise FederationError(FEDERATION_MALFORMED_FRAME, "导航 grant target 无效") from error
    if set(raw) == {"gateway_id", "uri", "fork_role"}:
        try:
            return ForkGrantTarget(**{key: _require_str(raw, key) for key in raw})
        except (TypeError, ValueError) as error:
            raise FederationError(FEDERATION_MALFORMED_FRAME, str(error)) from error
    if set(raw) == {"gateway_id", "resource_identity", "uri", "scope"}:
        try:
            return ContextGrantTarget(**{key: _require_str(raw, key) for key in raw})
        except (TypeError, ValueError) as error:
            raise FederationError(FEDERATION_MALFORMED_FRAME, str(error)) from error
    raise FederationError(FEDERATION_MALFORMED_FRAME, "grant target 字段闭集不一致")


def validate_grant_operation(
    target: GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget, operation: OperationKind,
) -> None:
    """operation 与 typed target 在所有签发及 relay 入口共用一个闭集。"""
    if isinstance(target, ForkGrantTarget):
        valid = operation == "fork_" + target.fork_role
    elif isinstance(target, (ContextGrantTarget, ResourceNavigationGrantTarget)):
        valid = operation == "read"
    else:
        valid = operation in {"send", "read", "wait", "reply"}
    if not valid:
        raise FederationError(FEDERATION_GRANT_KIND_MISMATCH, "grant operation 与 typed target capability 不一致")


@dataclass(frozen=True, slots=True)
class FederationGrant:
    """已签名的 grant 明文：``signature`` 覆盖除自身外的全部字段。"""

    grant_kind: GrantKind
    issuer_gateway_id: str
    origin_gateway_id: str
    audience_gateway_id: str
    transit_path: tuple[str, ...]
    request_id: str
    nonce: str
    expires_at: datetime
    issued_at: datetime
    principal_ref: str
    request_hash: str
    target: GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget | None = None
    operation: OperationKind | None = None
    source_gateway_id: str | None = None
    operation_invocation_id: str | None = None
    signature: str = ""

    def payload_without_signature(self) -> dict[str, object]:
        return {
            "grant_kind": self.grant_kind,
            "issuer_gateway_id": self.issuer_gateway_id,
            "origin_gateway_id": self.origin_gateway_id,
            "audience_gateway_id": self.audience_gateway_id,
            "transit_path": list(self.transit_path),
            "request_id": self.request_id,
            "nonce": self.nonce,
            "expires_at": self.expires_at.isoformat(),
            "issued_at": self.issued_at.isoformat(),
            "principal_ref": self.principal_ref,
            "request_hash": self.request_hash,
            "target": self.target.to_payload() if self.target is not None else None,
            "operation": self.operation,
            "source_gateway_id": self.source_gateway_id,
            "operation_invocation_id": self.operation_invocation_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.payload_without_signature())

    def encode(self) -> dict[str, object]:
        payload = self.payload_without_signature()
        payload["signature"] = self.signature
        return payload

    @classmethod
    def decode(cls, raw: object) -> FederationGrant:
        if not isinstance(raw, dict):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "grant 必须是 JSON 对象")
        try:
            grant_kind = raw["grant_kind"]
            if grant_kind not in ("discovery", "operation"):
                raise FederationError(
                    FEDERATION_GRANT_KIND_MISMATCH,
                    f"未知 grant_kind: {grant_kind!r}",
                )
            operation = raw.get("operation")
            if operation is not None and operation not in (
                "send",
                "read",
                "wait",
                "reply",
                "fork_source",
                "fork_target",
            ):
                raise FederationError(
                    FEDERATION_GRANT_KIND_MISMATCH, f"未知 operation: {operation!r}"
                )
            raw_target = raw.get("target")
            target: GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget | None = None
            if raw_target is not None:
                target = decode_grant_target(raw_target)
            raw_path = raw.get("transit_path")
            if not isinstance(raw_path, list) or not all(
                isinstance(item, str) and item for item in raw_path
            ):
                raise FederationError(
                    FEDERATION_GRANT_PATH_MISMATCH, "grant.transit_path 必须是字符串数组"
                )
            return cls(
                grant_kind=grant_kind,
                issuer_gateway_id=_require_str(raw, "issuer_gateway_id"),
                origin_gateway_id=_require_str(raw, "origin_gateway_id"),
                audience_gateway_id=_require_str(raw, "audience_gateway_id"),
                transit_path=tuple(raw_path),
                request_id=_require_str(raw, "request_id"),
                nonce=_require_str(raw, "nonce"),
                expires_at=_require_datetime(raw, "expires_at"),
                issued_at=_require_datetime(raw, "issued_at"),
                principal_ref=_require_str(raw, "principal_ref"),
                request_hash=_require_str(raw, "request_hash"),
                target=target,
                operation=operation,
                source_gateway_id=_optional_str(raw, "source_gateway_id"),
                operation_invocation_id=_optional_str(
                    raw, "operation_invocation_id"
                ),
                signature=_require_str(raw, "signature"),
            )
        except FederationError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise FederationError(
                FEDERATION_MALFORMED_FRAME, f"grant 字段非法: {error}"
            ) from error


def _require_str(raw: dict[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, f"grant.{field} 必须是非空字符串"
        )
    return value


def _optional_str(raw: dict[str, object], field: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, f"grant.{field} 必须是非空字符串或 null"
        )
    return value


def _require_datetime(raw: dict[str, object], field: str) -> datetime:
    value = raw.get(field)
    if not isinstance(value, str):
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, f"grant.{field} 必须是 ISO 时间字符串"
        )
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise FederationError(
            FEDERATION_MALFORMED_FRAME, f"grant.{field} 必须带时区"
        )
    return parsed


def sign_grant(
    *,
    private_key: Ed25519PrivateKey,
    grant_kind: GrantKind,
    issuer_gateway_id: str,
    origin_gateway_id: str,
    audience_gateway_id: str,
    transit_path: tuple[str, ...],
    request_id: str,
    principal_ref: str,
    request_preimage: dict[str, object],
    lifetime_seconds: float,
    target: GrantTarget | ContextGrantTarget | ForkGrantTarget | ResourceNavigationGrantTarget | None = None,
    operation: OperationKind | None = None,
    source_gateway_id: str | None = None,
    operation_invocation_id: str | None = None,
    now: datetime | None = None,
) -> FederationGrant:
    """签发 grant；``transit_path`` 首尾分别为 origin 与 target。"""

    if grant_kind == "operation" and (target is None or operation is None):
        raise ValueError("operation grant 必须同时提供 target 与 operation")
    if target is not None and operation is not None:
        validate_grant_operation(target, operation)
    if grant_kind == "discovery" and (target is not None or operation is not None):
        raise ValueError("discovery grant 不得绑定 operation 或 target")
    if lifetime_seconds <= 0:
        raise ValueError("grant lifetime 必须为正数")
    issued_at = now or datetime.now(UTC)
    unsigned = FederationGrant(
        grant_kind=grant_kind,
        issuer_gateway_id=issuer_gateway_id,
        origin_gateway_id=origin_gateway_id,
        audience_gateway_id=audience_gateway_id,
        transit_path=transit_path,
        request_id=request_id,
        nonce=secrets.token_urlsafe(24),
        expires_at=issued_at + timedelta(seconds=lifetime_seconds),
        issued_at=issued_at,
        principal_ref=principal_ref,
        request_hash=hashlib.sha256(_canonical_bytes(request_preimage)).hexdigest(),
        target=target,
        operation=operation,
        source_gateway_id=source_gateway_id,
        operation_invocation_id=operation_invocation_id,
    )
    signature = private_key.sign(unsigned.canonical_bytes())
    return FederationGrant(
        **{
            field: getattr(unsigned, field)
            for field in unsigned.__slots__
            if field != "signature"
        },
        signature=_b64url_encode(signature),
    )


def verify_grant(
    grant: FederationGrant,
    *,
    issuer: FederationPeerIdentity,
    local_gateway_id: str,
    expected_kind: GrantKind,
    request_preimage: dict[str, object],
    expected_origin: str | None = None,
    expected_path: tuple[str, ...] | None = None,
    now: datetime | None = None,
    minimum_remaining_seconds: float = 0.0,
) -> None:
    """校验签名、audience、path、kind、origin 与期限；失败即显式拒绝。"""

    if grant.issuer_gateway_id != issuer.gateway_id:
        raise FederationError(
            FEDERATION_GRANT_INVALID_SIGNATURE,
            "grant issuer 与当前 channel 绑定的 hub 身份不一致",
            detail={
                "issuer": grant.issuer_gateway_id,
                "bound": issuer.gateway_id,
            },
        )
    issuer.verify(grant.canonical_bytes(), _b64url_decode(grant.signature))
    if grant.request_hash != hashlib.sha256(_canonical_bytes(request_preimage)).hexdigest() or grant.request_id != request_preimage.get("request_id"):
        raise FederationError(
            FEDERATION_GRANT_INVALID_SIGNATURE,
            "grant 与实际请求的正文、选择或 request_id preimage 不一致",
        )
    if grant.grant_kind != expected_kind:
        raise FederationError(
            FEDERATION_GRANT_KIND_MISMATCH,
            f"grant 类型不符: expected={expected_kind}, actual={grant.grant_kind}",
        )
    if grant.audience_gateway_id != local_gateway_id:
        raise FederationError(
            FEDERATION_GRANT_AUDIENCE_MISMATCH,
            "grant audience 不是本 Gateway",
            detail={"audience": grant.audience_gateway_id, "local": local_gateway_id},
        )
    if expected_origin is not None and grant.origin_gateway_id != expected_origin:
        raise FederationError(
            FEDERATION_GRANT_ORIGIN_MISMATCH,
            "grant origin 与 channel 绑定的真实 origin 不一致",
            detail={"grant_origin": grant.origin_gateway_id, "channel_origin": expected_origin},
        )
    if expected_path is not None and grant.transit_path != expected_path:
        raise FederationError(
            FEDERATION_GRANT_PATH_MISMATCH,
            "grant transit path 与预期路径不一致",
            detail={"grant": list(grant.transit_path), "expected": list(expected_path)},
        )
    if len(grant.transit_path) - 2 > MAX_TRANSIT_GATEWAYS:
        raise FederationError(
            FEDERATION_GRANT_PATH_MISMATCH,
            "grant transit path 超出允许的 transit 上限",
            detail={"path": list(grant.transit_path)},
        )
    current = now or datetime.now(UTC)
    if grant.expires_at <= current:
        raise FederationError(
            FEDERATION_GRANT_EXPIRED,
            "grant 已过期",
            detail={"expires_at": grant.expires_at.isoformat()},
        )
    remaining = (grant.expires_at - current).total_seconds()
    if remaining + 1e-9 < minimum_remaining_seconds:
        raise FederationError(
            FEDERATION_GRANT_LIFETIME_INSUFFICIENT,
            "grant 剩余寿命不足以覆盖 effective timeout 与时钟偏差",
            detail={
                "remaining_seconds": remaining,
                "required_seconds": minimum_remaining_seconds,
            },
        )


def required_wait_grant_lifetime(
    *,
    effective_timeout_seconds: float,
    clock_skew_seconds: float = GRANT_CLOCK_SKEW_SECONDS,
) -> float:
    """wait grant 的剩余寿命下界：effective timeout + 时钟偏差。"""

    if effective_timeout_seconds <= 0:
        raise ValueError("effective timeout 必须为正数")
    return effective_timeout_seconds + max(clock_skew_seconds, 0.0)


__all__ = [
    "DISCOVERY_GRANT_LIFETIME_SECONDS",
    "GRANT_CLOCK_SKEW_SECONDS",
    "MAX_GATEWAY_HOPS",
    "MAX_TRANSIT_GATEWAYS",
    "ContextGrantTarget",
    "FederationGrant",
    "ForkGrantTarget",
    "GrantKind",
    "GrantTarget",
    "OperationKind",
    "ResourceNavigationGrantTarget",
    "decode_grant_target",
    "required_wait_grant_lifetime",
    "sign_grant",
    "verify_grant",
]
