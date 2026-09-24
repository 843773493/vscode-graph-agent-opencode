"""联邦请求载荷的字段校验与 transit 预算判定。

这些纯函数是 hub 与 spoke 两侧共享的唯一实现：discovery/relay 载荷结构、
visited set、hop/transit 上限与 target/operation 解析都在这里，避免两侧各自
写一份近似校验。任何不满足都抛携带稳定错误码的 :class:`FederationError`。
"""

from __future__ import annotations

import time

from app.gateway.federation.errors import (
    FEDERATION_DEADLINE_EXCEEDED,
    FEDERATION_GRANT_KIND_MISMATCH,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FEDERATION_TRANSIT_LIMIT_EXCEEDED,
    FederationError,
)
from app.gateway.federation.grants import (
    MAX_GATEWAY_HOPS,
    MAX_TRANSIT_GATEWAYS,
    GrantTarget,
    OperationKind,
)

# 单次 discovery/operation fan-out 的总 deadline 上界；超时必须响亮失败。
FEDERATION_DEADLINE_SECONDS = 8.0


def grant_preimage(request: dict[str, object]) -> dict[str, object]:
    """防重放 preimage：除 grant 外的全部业务参数（含 path/scope）。"""

    return {key: value for key, value in request.items() if key != "grant"}


def discovery_response(
    gateway_id: str, matches: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "gateway_id": gateway_id,
        "matches": matches,
        "ambiguity_count": len(matches),
    }


def require_discovery_scope(
    request: dict[str, object], *, local_gateway_id: str
) -> tuple[tuple[str, ...], int, int]:
    """校验 visited set 与协议声明的 transit/hop 上限，并拒绝自环递归。"""

    raw_visited = request.get("visited")
    if not isinstance(raw_visited, list) or not all(
        isinstance(item, str) and item for item in raw_visited
    ):
        raise FederationError(
            FEDERATION_TRANSIT_LIMIT_EXCEEDED, "visited 必须是字符串数组"
        )
    visited = tuple(raw_visited)
    max_transit = require_int(request, "max_transit_gateways")
    max_hops = require_int(request, "max_gateway_hops")
    if max_transit != MAX_TRANSIT_GATEWAYS or max_hops != MAX_GATEWAY_HOPS:
        raise FederationError(
            FEDERATION_TRANSIT_LIMIT_EXCEEDED,
            "请求声明的 transit 上限与协议合同不一致",
            detail={
                "max_transit_gateways": max_transit,
                "max_gateway_hops": max_hops,
            },
        )
    if local_gateway_id in visited:
        raise FederationError(
            FEDERATION_TRANSIT_LIMIT_EXCEEDED,
            "请求回到已访问 Gateway，拒绝递归",
            detail={"visited": list(visited)},
        )
    return visited, max_transit, max_hops


def assert_hop_budget(path: tuple[str, ...]) -> None:
    """``path`` 首尾为 origin 与 target；hops 与 transit 由同一路径长度决定。

    ``hops = len(path) - 1`` 与 ``transit = len(path) - 2`` 恒满足
    ``hops = transit + 1``，因此这是唯一一处路径预算判定，同时报告两个上限，
    避免调用方猜测究竟命中哪一个。
    """

    if len(path) < 2:
        raise FederationError(
            FEDERATION_TRANSIT_LIMIT_EXCEEDED,
            "联邦路径至少需要 origin 与 target 两个 Gateway",
            detail={"path": list(path)},
        )
    hops = len(path) - 1
    transit_gateways = len(path) - 2
    if hops > MAX_GATEWAY_HOPS or transit_gateways > MAX_TRANSIT_GATEWAYS:
        raise FederationError(
            FEDERATION_TRANSIT_LIMIT_EXCEEDED,
            "请求路径超出允许的 Gateway hop/唯一一次 transit 预算",
            detail={
                "path": list(path),
                "hops": hops,
                "max_gateway_hops": MAX_GATEWAY_HOPS,
                "transit_gateways": transit_gateways,
                "max_transit_gateways": MAX_TRANSIT_GATEWAYS,
            },
        )


def require_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FederationError(
            FEDERATION_TRANSIT_LIMIT_EXCEEDED, f"{field} 必须是整数"
        )
    return value


def require_operation(request: dict[str, object]) -> OperationKind:
    value = request.get("operation")
    if value not in ("send", "read", "wait", "reply"):
        raise FederationError(
            FEDERATION_GRANT_KIND_MISMATCH, f"未知 operation: {value!r}"
        )
    return value


def require_target(payload: dict[str, object]) -> GrantTarget:
    raw = payload.get("target")
    if not isinstance(raw, dict):
        raise FederationError(
            FEDERATION_TARGET_NOT_RESOLVABLE, "请求缺少 target 对象"
        )
    gateway_id = raw.get("gateway_id")
    workspace_id = raw.get("workspace_id")
    session_id = raw.get("session_id")
    for field, value in (
        ("gateway_id", gateway_id),
        ("workspace_id", workspace_id),
        ("session_id", session_id),
    ):
        if not isinstance(value, str) or not value:
            raise FederationError(
                FEDERATION_TARGET_NOT_RESOLVABLE,
                f"target.{field} 必须是非空字符串",
            )
    return GrantTarget(
        gateway_id=gateway_id, workspace_id=workspace_id, session_id=session_id
    )


def remaining_deadline(deadline_at: float | None) -> float:
    if deadline_at is None:
        return FEDERATION_DEADLINE_SECONDS
    remaining = deadline_at - time.time()
    if remaining <= 0:
        raise FederationError(FEDERATION_DEADLINE_EXCEEDED, "联邦操作已超过总 deadline")
    return remaining


__all__ = [
    "FEDERATION_DEADLINE_SECONDS",
    "assert_hop_budget",
    "discovery_response",
    "grant_preimage",
    "remaining_deadline",
    "require_discovery_scope",
    "require_int",
    "require_operation",
    "require_target",
]
