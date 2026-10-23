"""InboxAdmissionWorker 测试：恢复、幂等 claim、stale generation、失败重试。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.core.session_control_store import (
    CommunicationInboxRecord,
    SessionControlStore,
)
from app.core.session_lifecycle_gate import (
    SessionLifecycleGate,
    SessionOperationLease,
)
from app.services.orchestration.communication.inbox_admission_worker import (
    InboxAdmissionTarget,
    InboxAdmissionWorker,
    InboxExecutionBinding,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING

DEFAULT_CREATED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _new_execution_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


@dataclass
class FakeBinder:
    """可编程 fake binder：失败模式可注入，记录调用过的 wakeup_key。"""

    job_id: str | None = None
    fail_message: str | None = None
    seen_wakeup_keys: list[str] | None = None

    async def __call__(self, target: InboxAdmissionTarget) -> InboxExecutionBinding:
        if self.seen_wakeup_keys is not None:
            self.seen_wakeup_keys.append(target.inbox.wakeup_key)
        if self.fail_message is not None:
            raise RuntimeError(self.fail_message)
        return InboxExecutionBinding(
            job_id=self.job_id or _new_execution_job_id(),
            execution_id="exec-communication-test",
            turn_id=None,
        )


@dataclass
class StoreHarness:
    """worker 测试装具：store + 该库归属的合法 session_id。"""

    store: SessionControlStore
    session_id: str
    gate: SessionLifecycleGate

    @asynccontextmanager
    async def lease_settlement(
        self, session_id: str, store: SessionControlStore, lease: SessionOperationLease,
    ) -> AsyncIterator[bool]:
        assert session_id == self.session_id and store is self.store
        async with self.gate.exclusive(session_id):
            store.assert_lease_current(lease)
            yield True


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[StoreHarness]:
    session_id = f"ses_{create_uuid_hex()}"
    session_store = SessionControlStore(tmp_path / "session-control.sqlite")
    try:
        session_store.initialize_main_thread(
            f"thr_{create_uuid_hex()}", DEFAULT_CREATED_AT, TEST_THREAD_GRAPH_BINDING
        )
        session_store.initialize_fence("active", 1)
        yield StoreHarness(
            store=session_store,
            session_id=session_id,
            gate=SessionLifecycleGate(tmp_path),
        )
    finally:
        session_store.close()


def seed_pending_inbox(
    harness: StoreHarness,
    *,
    communication_id: str | None = None,
    payload_hash: str = "0" * 64,
) -> CommunicationInboxRecord:
    record, _was_created = harness.store.create_or_get_communication_inbox(
        session_id=harness.session_id,
        communication_id=communication_id or f"comm_{uuid.uuid4().hex}",
        source_gateway_id="gw_a",
        source_workspace_id="ws_shared",
        source_session_id=f"ses_{create_uuid_hex()}",
        source_thread_id=f"thr_{create_uuid_hex()}",
        target_thread_id=str(harness.store.get_main_thread()["thread_id"]),
        kind="result",
        reply_to_communication_id=None,
        payload_hash=payload_hash,
        content="fixture content",
        delivery_policy="after_turn",
        request_id="req-communication-test",
        target_gateway_id="gw_b",
        target_workspace_id="ws_b",
    )
    lease = harness.store.create_or_get_lease(
        operation_kind="communication_target",
        operation_identity="communication-target|" + record.communication_id,
        preimage_hash=record.payload_hash,
    )
    harness.store.mark_lease_settling(
        lease_id=lease.lease_id,
        expected_fencing_token=lease.fencing_token,
    )
    return record


def test_constructor_requires_binder(harness: StoreHarness) -> None:
    with pytest.raises(TypeError, match="binder"):
        InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement, store=harness.store, claim_owner="worker-a", binder=None)


async def test_startup_recovery_binds_pending_inbox(harness: StoreHarness) -> None:
    inbox = seed_pending_inbox(harness)
    worker = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store,
        claim_owner="worker-a",
        binder=FakeBinder(job_id=_new_execution_job_id()),
    )
    worker.start()
    attempts = (await worker.admit_pending_once())
    worker.stop()
    assert [attempt.outcome for attempt in attempts] == ["bound"]
    bound = harness.store.get_communication_inbox(inbox.communication_id)
    assert bound.state == "execution_bound"
    assert bound.job_id == attempts[0].bound_inbox.job_id
    assert bound.admission_claim_owner == "worker-a"


async def test_recovery_after_restart_binds_remaining_inboxes(
    harness: StoreHarness,
) -> None:
    first = seed_pending_inbox(harness)
    second = seed_pending_inbox(harness)
    binder = FakeBinder()
    worker = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store, claim_owner="worker-a", binder=binder
    )
    worker.start()
    (await worker.admit_pending_once())
    # 模拟 backend 重启后新 worker（同 owner 更高 generation 接管语义由
    # store claim 提供；这里只验证新一轮恢复仍只消费未绑定行）。
    third = seed_pending_inbox(
        harness,
        communication_id=f"comm_{uuid.uuid4().hex}",
        payload_hash="1" * 64,
    )
    attempts = (await worker.admit_pending_once())
    assert [attempt.communication_id for attempt in attempts] == [
        third.communication_id
    ]
    assert harness.store.get_communication_inbox(first.communication_id).state == (
        "execution_bound"
    )
    assert harness.store.get_communication_inbox(second.communication_id).state == (
        "execution_bound"
    )
    assert (
        harness.store.get_communication_inbox(third.communication_id).state
        == "execution_bound"
    )


async def test_duplicate_claim_by_same_owner_is_idempotent(
    harness: StoreHarness,
) -> None:
    inbox = seed_pending_inbox(harness)
    # binder 失败：claim 已持有但 state 保持 target_accepted（可恢复）。
    binder = FakeBinder(fail_message="job service 暂不可用")
    worker = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store, claim_owner="worker-a", binder=binder
    )
    worker.start()
    with pytest.raises(RuntimeError, match="显式失败"):
        (await worker.admit_pending_once())
    # 同 owner 同 generation 重入 claim 幂等（崩溃恢复重入契约面）。
    reclaimed = harness.store.claim_communication_inbox_admission(
        inbox.communication_id, claim_owner="worker-a", claim_generation=1
    )
    assert reclaimed.admission_claim_owner == "worker-a"
    assert reclaimed.state == "target_accepted"


async def test_two_workers_claim_conflict_classified(harness: StoreHarness) -> None:
    seed_pending_inbox(harness)
    # worker-a 先 claim（binder 失败保持 target_accepted 可恢复）。
    failing = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store,
        claim_owner="worker-a",
        binder=FakeBinder(fail_message="job service 暂不可用"),
    )
    failing.start()
    with pytest.raises(RuntimeError, match="显式失败"):
        (await failing.admit_pending_once())
    # worker-b 不同 owner：claim 冲突按并发契约分类，不覆盖不重基。
    succeeding = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store, claim_owner="worker-b", binder=FakeBinder()
    )
    succeeding.start()
    attempts = (await succeeding.admit_pending_once())
    assert [attempt.outcome for attempt in attempts] == ["skipped_claim_held"]


async def test_stale_generation_is_rejected(harness: StoreHarness) -> None:
    inbox = seed_pending_inbox(harness)
    newer = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store,
        claim_owner="worker-a",
        claim_generation=2,
        binder=FakeBinder(),
    )
    newer.start()
    assert (await newer.admit_pending_once())[0].outcome == "bound"
    # bound 后重复 claim 直接拒绝（非 target_accepted）。
    with pytest.raises(RuntimeError, match="非 target_accepted"):
        harness.store.claim_communication_inbox_admission(
            inbox.communication_id, claim_owner="worker-a", claim_generation=3
        )
    # 同 owner 更高 generation 持有 claim 后，低 generation 重入被拒绝
    # （generation 只增不减，fail closed）。
    fresh = seed_pending_inbox(harness)
    harness.store.claim_communication_inbox_admission(
        fresh.communication_id, claim_owner="worker-a", claim_generation=3
    )
    stale = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store,
        claim_owner="worker-a",
        claim_generation=1,
        binder=FakeBinder(),
    )
    stale.start()
    with pytest.raises(RuntimeError, match="generation"):
        (await stale.admit_pending_once())


async def test_binder_failure_keeps_recoverable_then_retries(
    harness: StoreHarness,
) -> None:
    inbox = seed_pending_inbox(harness)
    failing = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store,
        claim_owner="worker-a",
        binder=FakeBinder(fail_message="job service 暂不可用"),
    )
    failing.start()
    with pytest.raises(RuntimeError, match="job service 暂不可用"):
        (await failing.admit_pending_once())
    current = harness.store.get_communication_inbox(inbox.communication_id)
    assert current.state == "target_accepted"
    assert current.last_error is not None
    assert "job service 暂不可用" in current.last_error
    # 失败后重试：同 owner 同 generation claim 幂等重入，绑定成功。
    recovered = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store,
        claim_owner="worker-a",
        binder=FakeBinder(job_id=_new_execution_job_id()),
    )
    recovered.start()
    attempts = (await recovered.admit_pending_once())
    assert [attempt.outcome for attempt in attempts] == ["bound"]
    assert harness.store.get_communication_inbox(inbox.communication_id).state == (
        "execution_bound"
    )


async def test_binder_receives_frozen_wakeup_key(harness: StoreHarness) -> None:
    inbox = seed_pending_inbox(harness)
    seen: list[str] = []
    binder = FakeBinder(seen_wakeup_keys=seen)
    worker = InboxAdmissionWorker(session_id=harness.session_id, lease_settlement=harness.lease_settlement,
        store=harness.store, claim_owner="worker-a", binder=binder
    )
    worker.start()
    (await worker.admit_pending_once())
    assert seen == [inbox.wakeup_key]
