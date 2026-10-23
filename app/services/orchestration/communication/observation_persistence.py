"""冷观察持久身份、结果错误和 operation lease 状态转换。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict

from app.core.session_control_communication_ledger.observations import (
    RemoteObservation,
    RemoteObservationError,
    canonical_json,
    observation_error_code,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SESSION_OPERATION_LEASE_TERMINAL_STATES,
    SessionOperationLease,
)
from app.services.business.communication.addresses import GlobalThreadAddress


def _observation_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _observation_page_identity(
    source: GlobalThreadAddress,
    source_call_id: str,
) -> str:
    return "page_" + _observation_hash([asdict(source), source_call_id])


def _frozen_read_parameters(query: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in query.items() if key not in {"cursor", "revision"}}


def _get_or_takeover_observation_lease(
    store: SessionControlStore,
    identity: str,
    preimage: object,
) -> SessionOperationLease:
    previous = store.find_lease_by_operation(identity)
    lease = store.create_or_get_lease(
        operation_kind="remote_observation",
        operation_identity=identity,
        preimage_hash=_observation_hash(preimage),
        recovery_ref=identity,
    )
    if previous is not None and lease.state in {"active", "settling"}:
        # 调用方已独占同一 OS holder 锁；旧 holder 停止后才能换 durable token。
        lease = store.takeover_lease(
            lease_id=lease.lease_id,
            expected_fencing_token=lease.fencing_token,
        )
    if lease.state == "active":
        lease = store.mark_lease_settling(
            lease_id=lease.lease_id,
            expected_fencing_token=lease.fencing_token,
        )
    return lease


def _require_current_observation_lease(
    store: SessionControlStore,
    lease: SessionOperationLease,
    *,
    allow_completed: bool = False,
) -> SessionOperationLease:
    try:
        current = store.get_lease(lease.lease_id)
    except KeyError:
        raise RemoteObservationError("operation-retry-expired") from None
    allowed_states = {"settling", "completed"} if allow_completed else {"settling"}
    if current.fencing_token != lease.fencing_token or current.state not in allowed_states:
        raise RemoteObservationError("operation-retry-expired")
    return current


def _settle_observation_lease(
    store: SessionControlStore,
    lease: SessionOperationLease,
    *,
    outcome: str = "completed",
) -> None:
    try:
        current = store.get_lease(lease.lease_id)
    except KeyError:
        raise RemoteObservationError("operation-retry-expired") from None
    if current.fencing_token != lease.fencing_token:
        raise RemoteObservationError("operation-retry-expired")
    if current.state in SESSION_OPERATION_LEASE_TERMINAL_STATES:
        return
    if current.state == "active":
        current = store.mark_lease_settling(
            lease_id=current.lease_id,
            expected_fencing_token=current.fencing_token,
        )
    store.settle_lease(
        lease_id=current.lease_id,
        expected_fencing_token=current.fencing_token,
        outcome=outcome,
    )


def _cancel_observation_lease_if_present(
    store: SessionControlStore,
    identity: str,
) -> None:
    lease = store.find_lease_by_operation(identity)
    if lease is not None:
        # 删除 owner 已提交真实取消证明；原 token 终态即可阻断晚回调，不冒称 holder 失效。
        _settle_observation_lease(store, lease, outcome="cancelled")


def _observation_lease_identities(
    store: SessionControlStore,
    observation: RemoteObservation,
) -> tuple[str, ...]:
    return (
        observation.side + "-observation|" + observation.observation_id,
        *(
            observation.side + "-page|" + page.page_identity
            for page in store.list_remote_pages(
                observation.observation_id,
                side=observation.side,
            )
        ),
    )


def _all_observation_leases_terminal(
    store: SessionControlStore,
    identities: tuple[str, ...],
) -> bool:
    for identity in identities:
        lease = store.find_lease_by_operation(identity)
        if lease is None or lease.state not in SESSION_OPERATION_LEASE_TERMINAL_STATES:
            return False
        if lease.operation_kind != "remote_observation":
            raise RuntimeError("observation 引用了其它 operation kind 的 lease")
    return True


def _raise_for_observation_error(value: dict[str, object]) -> dict[str, object]:
    code = observation_error_code(value.get("error"))
    if code is not None:
        raise RemoteObservationError(code)
    return value
