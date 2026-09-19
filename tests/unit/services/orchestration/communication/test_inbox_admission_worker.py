"""InboxAdmissionWorker 测试：恢复、幂等 claim、stale generation、失败重试。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_control_store import (
    CommunicationInboxRecord,
    SessionControlStore,
)
from app.services.orchestration.communication.inbox_admission_worker import (
    InboxAdmissionTarget,
    InboxAdmissionWorker,
    InboxExecutionBinding,
)

DEFAULT_CREATED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def make_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


@dataclass
class FakeBinder:
    """可编程 fake binder：失败模式可注入，记录调用过的 wakeup_key。"""

    job_id: str | None = None
    fail_message: str | None = None
    seen_wakeup_keys: list[str] | None = None

    def __call__(self, target: InboxAdmissionTarget) -> InboxExecutionBinding:
        if self.seen_wakeup_keys is not None:
            self.seen_wakeup_keys.append(target.inbox.wakeup_key)
        if self.fail_message is not None:
            raise RuntimeError(self.fail_message)
        return InboxExecutionBinding(
            job_id=self.job_id or make_job_id(), turn_id=None
        )


@dataclass
class StoreHarness:
    """worker 测试装具：store + 该库归属的合法 session_id。"""

    store: SessionControlStore
    session_id: str


@pytest.fixture
def harness(tmp_path: Path) -> StoreHarness:
    session_id = f"ses_{uuid.uuid4().hex}"
    created = SessionControlStore(tmp_path / "session-control.sqlite")
    created.initialize_main_thread(f"thr_{uuid.uuid4().hex}", DEFAULT_CREATED_AT)
    created.initialize_fence("active", 1)
    yield StoreHarness(store=created, session_id=session_id)
    created.close()


def seed_inbox(
    harness: StoreHarness,
    *,
    communication_id: str | None = None,
) -> CommunicationInboxRecord:
    record, _created = harness.store.create_or_get_communication_inbox(
        session_id=harness.session_id,
        communication_id=communication_id or f"comm_{uuid.uuid4().hex}",
        source_gateway_id="gw_a",
        source_workspace_id="ws_shared",
        source_session_id=f"ses_{uuid.uuid4().hex}",
        source_thread_id=f"thr_{uuid.uuid4().hex}",
        target_thread_id=str(harness.store.get_main_thread()["thread_id"]),
        kind="result",
        reply_to_communication_id=None,
        payload_hash="0" * 64,
    )
    return record


def seed_inbox_with_hash(
    harness: StoreHarness,
    *,
    communication_id: str,
    payload_hash: str,
) -> CommunicationInboxRecord:
    record, _created = harness.store.create_or_get_communication_inbox(
        session_id=harness.session_id,
        communication_id=communication_id,
        source_gateway_id="gw_a",
        source_workspace_id="ws_shared",
        source_session_id=f"ses_{uuid.uuid4().hex}",
        source_thread_id=f"thr_{uuid.uuid4().hex}",
        target_thread_id=str(harness.store.get_main_thread()["thread_id"]),
        kind="result",
        reply_to_communication_id=None,
        payload_hash=payload_hash,
    )
    return record


async def test_start_requires_binder(harness: StoreHarness) -> None:
    worker = InboxAdmissionWorker(store=harness.store, claim_owner="worker-a")
    with pytest.raises(RuntimeError, match="unavailable"):
        worker.start()
    with pytest.raises(RuntimeError, match="未启动"):
        worker.admit_pending_once()


async def test_startup_recovery_binds_pending_inbox(harness: StoreHarness) -> None:
    inbox = seed_inbox(harness)
    worker = InboxAdmissionWorker(
        store=harness.store, claim_owner="worker-a", binder=FakeBinder(job_id=make_job_id())
    )
    worker.start()
    attempts = worker.admit_pending_once()
    worker.stop()
    assert [attempt.outcome for attempt in attempts] == ["bound"]
    bound = harness.store.get_communication_inbox(inbox.communication_id)
    assert bound.state == "execution_bound"
    assert bound.job_id == attempts[0].bound_inbox.job_id
    assert bound.admission_claim_owner == "worker-a"


async def test_recovery_after_restart_binds_remaining_inboxes(
    harness: StoreHarness,
) -> None:
    first = seed_inbox(harness)
    second = seed_inbox(harness)
    binder = FakeBinder()
    worker = InboxAdmissionWorker(
        store=harness.store, claim_owner="worker-a", binder=binder
    )
    worker.start()
    worker.admit_pending_once()
    # 模拟 backend 重启后新 worker（同 owner 更高 generation 接管语义由
    # store claim 提供；这里只验证新一轮恢复仍只消费未绑定行）。
    third = seed_inbox_with_hash(
        harness,
        communication_id=f"comm_{uuid.uuid4().hex}",
        payload_hash="1" * 64,
    )
    attempts = worker.admit_pending_once()
    assert [attempt.communication_id for attempt in attempts] == [
        third.communication_id
    ]
    assert harness.store.get_communication_inbox(first.communication_id).state == (
        "execution_bound"
    )
    assert harness.store.get_communication_inbox(second.communication_id).state == (
        "execution_bound"
    )
    assert harness.store.get_communication_inbox(
        third.communication_id
    ).state == "execution_bound"


async def test_duplicate_claim_by_same_owner_is_idempotent(
    harness: StoreHarness,
) -> None:
    inbox = seed_inbox(harness)
    # binder 失败：claim 已持有但 state 保持 target_accepted（可恢复）。
    binder = FakeBinder(fail_message="job service 暂不可用")
    worker = InboxAdmissionWorker(
        store=harness.store, claim_owner="worker-a", binder=binder
    )
    worker.start()
    with pytest.raises(RuntimeError, match="显式失败"):
        worker.admit_pending_once()
    # 同 owner 同 generation 重入 claim 幂等（崩溃恢复重入契约面）。
    reclaimed = harness.store.claim_communication_inbox_admission(
        inbox.communication_id, claim_owner="worker-a", claim_generation=1
    )
    assert reclaimed.admission_claim_owner == "worker-a"
    assert reclaimed.state == "target_accepted"


async def test_two_workers_claim_conflict_classified(harness: StoreHarness) -> None:
    seed_inbox(harness)
    # worker-a 先 claim（binder 失败保持 target_accepted 可恢复）。
    failing = InboxAdmissionWorker(
        store=harness.store,
        claim_owner="worker-a",
        binder=FakeBinder(fail_message="job service 暂不可用"),
    )
    failing.start()
    with pytest.raises(RuntimeError, match="显式失败"):
        failing.admit_pending_once()
    # worker-b 不同 owner：claim 冲突按并发契约分类，不覆盖不重基。
    succeeding = InboxAdmissionWorker(
        store=harness.store, claim_owner="worker-b", binder=FakeBinder()
    )
    succeeding.start()
    attempts = succeeding.admit_pending_once()
    assert [attempt.outcome for attempt in attempts] == ["skipped_claim_held"]


async def test_stale_generation_is_rejected(harness: StoreHarness) -> None:
    inbox = seed_inbox(harness)
    newer = InboxAdmissionWorker(
        store=harness.store, claim_owner="worker-a", claim_generation=2, binder=FakeBinder()
    )
    newer.start()
    assert newer.admit_pending_once()[0].outcome == "bound"
    # bound 后重复 claim 直接拒绝（非 target_accepted）。
    with pytest.raises(RuntimeError, match="非 target_accepted"):
        harness.store.claim_communication_inbox_admission(
            inbox.communication_id, claim_owner="worker-a", claim_generation=3
        )
    # 同 owner 更高 generation 持有 claim 后，低 generation 重入被拒绝
    # （generation 只增不减，fail closed）。
    fresh = seed_inbox(harness)
    harness.store.claim_communication_inbox_admission(
        fresh.communication_id, claim_owner="worker-a", claim_generation=3
    )
    stale = InboxAdmissionWorker(
        store=harness.store, claim_owner="worker-a", claim_generation=1, binder=FakeBinder()
    )
    stale.start()
    with pytest.raises(RuntimeError, match="generation"):
        stale.admit_pending_once()


async def test_binder_failure_keeps_recoverable_then_retries(
    harness: StoreHarness,
) -> None:
    inbox = seed_inbox(harness)
    failing = InboxAdmissionWorker(
        store=harness.store,
        claim_owner="worker-a",
        binder=FakeBinder(fail_message="job service 暂不可用"),
    )
    failing.start()
    with pytest.raises(RuntimeError, match="job service 暂不可用"):
        failing.admit_pending_once()
    current = harness.store.get_communication_inbox(inbox.communication_id)
    assert current.state == "target_accepted"
    assert current.last_error is not None
    assert "job service 暂不可用" in current.last_error
    # 失败后重试：同 owner 同 generation claim 幂等重入，绑定成功。
    recovered = InboxAdmissionWorker(
        store=harness.store, claim_owner="worker-a", binder=FakeBinder(job_id=make_job_id())
    )
    recovered.start()
    attempts = recovered.admit_pending_once()
    assert [attempt.outcome for attempt in attempts] == ["bound"]
    assert harness.store.get_communication_inbox(inbox.communication_id).state == (
        "execution_bound"
    )


async def test_binder_receives_frozen_wakeup_key(harness: StoreHarness) -> None:
    inbox = seed_inbox(harness)
    seen: list[str] = []
    binder = FakeBinder(seen_wakeup_keys=seen)
    worker = InboxAdmissionWorker(store=harness.store, claim_owner="worker-a", binder=binder)
    worker.start()
    worker.admit_pending_once()
    assert seen == [inbox.wakeup_key]
