"""冷观察持久身份、结果错误和 operation lease 状态转换。"""

from __future__ import annotations

import hashlib
import logging
from contextlib import closing
from dataclasses import asdict
from typing import TYPE_CHECKING

from app.core.exceptions import NotFoundError
from app.core.session_control_communication_ledger.observations import (
    RemoteObservation,
    RemoteObservationError,
    canonical_json,
    observation_error_code,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SESSION_OPERATION_LEASE_TERMINAL_STATES,
    SessionDeletionPendingError,
    SessionOperationLease,
)
from app.services.business.communication.addresses import GlobalThreadAddress

if TYPE_CHECKING:
    from app.schemas.internal_v2.communication import CommunicationObservationRequest
    from app.services.orchestration.communication.service import (
        SessionCommunicationService,
    )

logger = logging.getLogger(__name__)


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


async def record_observation_failure(
    communication: SessionCommunicationService, session_id: str, request: CommunicationObservationRequest,
    root_lease: SessionOperationLease, page_lease: SessionOperationLease,
    error: Exception, *, side: str,
) -> None:
    code = error.code if isinstance(error, RemoteObservationError) else "read-snapshot-mismatch"
    result = {"error": RemoteObservationError(code).to_wire()}
    if not isinstance(error, RemoteObservationError):
        facts: dict[str, object] = {"type": type(error).__name__}
        for name in ("errno", "winerror", "sqlite_errorcode"):
            if isinstance(system_code := getattr(error, name, None), int):
                facts[name] = system_code
        result["owner_failure"] = facts
        logger.error(
            "冷观察 owner 失败: session_id=%s, side=%s, operation=%s, observation_id=%s, page_identity=%s；完整诊断见异常链",
            session_id, side, request.operation, request.observation_id, request.page_identity,
            exc_info=error,
        )
    try:
        async with communication._sessions.session_write_admission(session_id):
            with closing(communication._store(session_id)) as store:
                try:
                    current = store.get_lease(page_lease.lease_id)
                except KeyError:
                    raise RemoteObservationError("operation-retry-expired") from None
                if current.fencing_token != page_lease.fencing_token:
                    raise RemoteObservationError("operation-retry-expired")
                if current.state in SESSION_OPERATION_LEASE_TERMINAL_STATES:
                    return
                _require_current_observation_lease(store, page_lease)
                root = _require_current_observation_lease(store, root_lease, allow_completed=True)
                store.commit_remote_page(request.observation_id, side=side, page_identity=request.page_identity, result=result)
                outcome = "cancelled" if code == "target-deleted" else "failed"
                _settle_observation_lease(store, page_lease, outcome=outcome)
                if root.state != "completed":
                    store.commit_remote_observation(request.observation_id, side=side, result=result)
                    _settle_observation_lease(store, root_lease, outcome=outcome)
    except (SessionDeletionPendingError, NotFoundError):
        # 原生产删除 callback 已在同一 gate 中提交并终结这些记录。
        return

