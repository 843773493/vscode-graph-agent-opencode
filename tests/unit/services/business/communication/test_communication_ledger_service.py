"""CommunicationLedgerService 测试：gate→lease→store 链路与双端 reply 闭环。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_control_store import (
    SessionControlStore,
    derive_communication_admission_identity,
)
from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.services.business.communication.addresses import (
    GlobalThreadAddress,
    ResolvedMainTargetBinding,
)
from app.services.business.communication.admission import (
    CommunicationPayload,
    CommunicationSendRequest,
)
from app.services.business.communication.ledger import CommunicationLedgerService

DEFAULT_CREATED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class SessionHarness:
    """单 session 测试装具：store + gate + facade 绑定同一 session。"""

    def __init__(self, tmp_path: Path, name: str) -> None:
        self.session_id = f"ses_{uuid.uuid4().hex}"
        self.main_thread_id = f"thr_{uuid.uuid4().hex}"
        self.store = SessionControlStore(tmp_path / name / "session-control.sqlite")
        self.store.initialize_main_thread(self.main_thread_id, DEFAULT_CREATED_AT)
        self.store.initialize_fence("active", 1)
        self.gate = SessionLifecycleGate(tmp_path)
        self.facade = CommunicationLedgerService(
            session_id=self.session_id,
            store=self.store,
            gate=self.gate,
        )

    def address(self) -> GlobalThreadAddress:
        return GlobalThreadAddress(
            gateway_id="gw_a",
            workspace_id="ws_shared",
            session_id=self.session_id,
            thread_id=self.main_thread_id,
        )

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[SessionHarness, SessionHarness]]:
    """双 session 装具：A/B 各自独立 gate 与 control store。"""
    harness_a = SessionHarness(tmp_path, "session-a")
    harness_b = SessionHarness(tmp_path, "session-b")
    yield harness_a, harness_b
    harness_a.close()
    harness_b.close()


def make_send_request(
    source: GlobalThreadAddress, target: GlobalThreadAddress
) -> CommunicationSendRequest:
    return CommunicationSendRequest(
        source=source,
        target_binding=ResolvedMainTargetBinding(
            target_session_id=target.session_id,
            target_main_thread_id=target.thread_id,
            target=target,
        ),
        send_operation_id=f"send-op-{uuid.uuid4().hex[:8]}",
        payload=CommunicationPayload(content="周报数据"),
    )


async def test_admit_outgoing_send_creates_lease_and_outbox(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    request = make_send_request(harness_a.address(), harness_b.address())
    decision = await harness_a.facade.admit_outgoing_send(
        request=request, communication_id=f"comm_{uuid.uuid4().hex}"
    )
    assert decision.is_duplicate is False
    leases = harness_a.store.list_non_terminal_leases()
    assert any(
        lease.operation_kind == "communication_source"
        and lease.operation_identity
        == f"communication-source|{request.send_operation_id}"
        for lease in leases
    )
    outbox_row = harness_a.store.connection.execute(
        "SELECT state FROM communication_outbox WHERE send_operation_id = ?",
        (request.send_operation_id,),
    ).fetchone()
    assert outbox_row is not None
    assert str(outbox_row["state"]) == "accepted"


async def test_admit_outgoing_send_duplicate_keeps_lease_token(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    request = make_send_request(harness_a.address(), harness_b.address())
    communication_id = f"comm_{uuid.uuid4().hex}"
    first = await harness_a.facade.admit_outgoing_send(
        request=request, communication_id=communication_id
    )
    second = await harness_a.facade.admit_outgoing_send(
        request=request, communication_id=communication_id
    )
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.communication_id == first.communication_id
    lease = harness_a.store.find_lease_by_operation(
        f"communication-source|{request.send_operation_id}"
    )
    assert lease is not None
    assert lease.fencing_token == 1


async def test_admit_outgoing_send_payload_drift_conflicts(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    request = make_send_request(harness_a.address(), harness_b.address())
    communication_id = f"comm_{uuid.uuid4().hex}"
    await harness_a.facade.admit_outgoing_send(
        request=request, communication_id=communication_id
    )
    drifted = CommunicationSendRequest(
        source=request.source,
        target_binding=request.target_binding,
        send_operation_id=request.send_operation_id,
        payload=CommunicationPayload(content="改写后的内容"),
    )
    with pytest.raises(RuntimeError, match="preimage"):
        await harness_a.facade.admit_outgoing_send(
            request=drifted, communication_id=communication_id
        )


async def test_accept_incoming_send_creates_target_lease_and_inbox(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    communication_id = f"comm_{uuid.uuid4().hex}"
    payload = CommunicationPayload(content="周报数据")
    inbox = await harness_b.facade.accept_incoming_send(
        communication_id=communication_id,
        source=harness_a.address(),
        target=harness_b.address(),
        payload=payload,
    )
    assert inbox.state == "target_accepted"
    admission_id, wakeup_key = derive_communication_admission_identity(
        communication_id, inbox.payload_hash
    )
    assert inbox.admission_id == admission_id
    assert inbox.wakeup_key == wakeup_key
    leases = harness_b.store.list_non_terminal_leases()
    assert any(
        lease.operation_kind == "communication_target"
        and lease.operation_identity == f"communication-target|{communication_id}"
        for lease in leases
    )


async def test_accept_incoming_send_duplicate_is_idempotent(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    communication_id = f"comm_{uuid.uuid4().hex}"
    payload = CommunicationPayload(content="周报数据")
    first = await harness_b.facade.accept_incoming_send(
        communication_id=communication_id,
        source=harness_a.address(),
        target=harness_b.address(),
        payload=payload,
    )
    second = await harness_b.facade.accept_incoming_send(
        communication_id=communication_id,
        source=harness_a.address(),
        target=harness_b.address(),
        payload=payload,
    )
    assert second.communication_id == first.communication_id
    assert second.admission_id == first.admission_id
    assert second.state == "target_accepted"


async def test_accept_rejects_foreign_target_session(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    with pytest.raises(ValueError, match="facade 绑定的 Session"):
        await harness_a.facade.accept_incoming_send(
            communication_id=f"comm_{uuid.uuid4().hex}",
            source=harness_b.address(),
            target=harness_b.address(),
            payload=CommunicationPayload(content="x"),
        )


async def test_deleting_gate_rejects_new_admission(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    assert harness_a.store.cas_fence_transition(1, "deleting") is True
    with pytest.raises(RuntimeError, match="fence 非 active"):
        await harness_a.facade.admit_outgoing_send(
            request=make_send_request(harness_a.address(), harness_b.address()),
            communication_id=f"comm_{uuid.uuid4().hex}",
        )


async def test_reply_round_trip_and_forgery_rejected(
    workspace: tuple[SessionHarness, SessionHarness],
) -> None:
    harness_a, harness_b = workspace
    question_id = f"comm_{uuid.uuid4().hex}"
    await harness_a.facade.admit_outgoing_send(
        request=make_send_request(harness_a.address(), harness_b.address()),
        communication_id=question_id,
    )
    await harness_b.facade.accept_incoming_send(
        communication_id=question_id,
        source=harness_a.address(),
        target=harness_b.address(),
        payload=CommunicationPayload(content="周报数据"),
    )
    reply_id = f"comm_{uuid.uuid4().hex}"
    await harness_b.facade.admit_outgoing_send(
        request=CommunicationSendRequest(
            source=harness_b.address(),
            target_binding=ResolvedMainTargetBinding(
                target_session_id=harness_a.session_id,
                target_main_thread_id=harness_a.main_thread_id,
                target=harness_a.address(),
            ),
            send_operation_id=f"send-op-{uuid.uuid4().hex[:8]}",
            payload=CommunicationPayload(
                content="回复", kind="reply", reply_to_communication_id=question_id
            ),
        ),
        communication_id=reply_id,
    )
    accepted = await harness_a.facade.accept_incoming_send(
        communication_id=reply_id,
        source=harness_b.address(),
        target=harness_a.address(),
        payload=CommunicationPayload(
            content="回复", kind="reply", reply_to_communication_id=question_id
        ),
    )
    assert accepted.state == "target_accepted"
    # 伪造：reply_to 指向本库不存在的 outbox → fail closed
    forged_id = f"comm_{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError, match="无法在本 session outbox 中证明"):
        await harness_a.facade.accept_incoming_send(
            communication_id=forged_id,
            source=harness_b.address(),
            target=harness_a.address(),
            payload=CommunicationPayload(
                content="伪造回复",
                kind="reply",
                reply_to_communication_id=f"comm_{uuid.uuid4().hex}",
            ),
        )
