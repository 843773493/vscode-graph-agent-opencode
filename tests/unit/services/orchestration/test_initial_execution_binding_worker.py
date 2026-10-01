"""InitialExecutionBindingWorker 单测（OpenSpec 8.5-B，R23）。

覆盖任务书 §3 worker 面：binder 成功、binder 异常、成功后提交前崩溃重试、
两个 worker 并发、无 binder 明确失败、identity 漂移拒绝。全部用例使用
tmp_path 独立 store，不触碰项目根目录。
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_control_store import SessionControlStore
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBindingError,
    InitialExecutionBindingTarget,
    InitialExecutionBindingWorker,
    InitialExecutionBindOutcome,
)

DEFAULT_CREATED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex}"


def make_session_id() -> str:
    return f"ses_{uuid.uuid4().hex}"


@pytest.fixture
def store(tmp_path: Path) -> SessionControlStore:
    created = SessionControlStore(tmp_path / "session-control.sqlite")
    yield created
    created.close()


def prepare_published_intent(store: SessionControlStore):
    """published record + pending intent（identity 由 admission key 派生）。"""
    store.initialize_main_thread(make_thread_id(), DEFAULT_CREATED_AT)
    store.initialize_fence("active", 1)
    graph_binding = json.dumps(
        {
            "graph_id": "deep-agent",
            "graph_revision": "rev-1",
            "graph_schema_hash": "sha256:aa",
            "capability_profile_hash": "sha256:bb",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    capability_profile = json.dumps(
        {"tools": ["read_file"]}, sort_keys=True, separators=(",", ":")
    )
    record = store.create_or_get_thread_creation_record(
        idempotency_key="key-1",
        initial_state="running",
        preimage_hash="a" * 64,
        graph_binding=graph_binding,
        capability_profile=capability_profile,
        created_at=DEFAULT_CREATED_AT,
        task_seed=json.dumps(
            {"task": "做一件事"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        task_reference=None,
    )
    store.freeze_thread_creation_artifact_manifest(
        "key-1", artifact_manifest="{}", artifact_manifest_hash="c" * 64
    )
    store.publish_thread_creation_record("key-1")
    return store.create_or_get_initial_execution_intent(
        admission_idempotency_key="key-1",
        session_id=make_session_id(),
        thread_id=record.child_thread_id,
        initial_state="running",
        creation_idempotency_key="key-1",
    )


def identity_binder(
    target: InitialExecutionBindingTarget,
) -> InitialExecutionBindOutcome:
    """标准成功 binder：原样返回冻结 identity。"""
    return InitialExecutionBindOutcome(
        execution_binding_id=target.execution_binding_id,
        job_id=target.job_id,
    )


def make_worker(
    store: SessionControlStore,
    *,
    claim_owner: str = "worker-a",
    binder=identity_binder,
) -> InitialExecutionBindingWorker:
    """构造已注入 binder 的 worker（binder=None 用于 unavailable 用例）。"""
    return InitialExecutionBindingWorker(
        store=store,
        claim_owner=claim_owner,
        claim_generation=1,
        binder=binder,
    )


def test_bind_success_marks_bound(store: SessionControlStore) -> None:
    """binder 成功且 identity 一致 → mark bound；pending 索引清空。"""
    intent = prepare_published_intent(store)
    seen_targets: list[InitialExecutionBindingTarget] = []

    def capturing_binder(
        target: InitialExecutionBindingTarget,
    ) -> InitialExecutionBindOutcome:
        seen_targets.append(target)
        return identity_binder(target)

    worker = make_worker(store, binder=capturing_binder)
    worker.start()
    attempts = worker.bind_pending_once()
    assert [attempt.outcome for attempt in attempts] == ["bound"]
    bound = store.get_initial_execution_intent("key-1")
    assert bound.state == "bound"
    assert bound.execution_binding_id == intent.execution_binding_id
    assert bound.job_id == intent.job_id
    assert store.list_pending_initial_execution_intents() == ()
    # binder 输入携带冻结 intent 与稳定 identity。
    assert len(seen_targets) == 1
    assert seen_targets[0].intent.admission_idempotency_key == "key-1"
    assert seen_targets[0].execution_binding_id == intent.execution_binding_id
    assert seen_targets[0].job_id == intent.job_id


def test_binder_exception_recorded_and_raised(
    store: SessionControlStore,
) -> None:
    """binder 异常 → last_error 记录、intent 保持 pending、显式抛出。"""
    prepare_published_intent(store)

    def failing_binder(
        target: InitialExecutionBindingTarget,
    ) -> InitialExecutionBindOutcome:
        raise RuntimeError("binder boom: graph unavailable")

    worker = make_worker(store, binder=failing_binder)
    worker.start()
    with pytest.raises(InitialExecutionBindingError, match="binder boom"):
        worker.bind_pending_once()
    intent = store.get_initial_execution_intent("key-1")
    assert intent.state == "pending"
    assert "binder boom" in str(intent.last_error)
    assert intent.claim_owner == "worker-a"
    # 同 claim 幂等重入恢复：binder 成功后完成绑定。
    recovered = make_worker(store)
    recovered.start()
    attempts = recovered.bind_pending_once()
    assert [attempt.outcome for attempt in attempts] == ["bound"]
    assert store.get_initial_execution_intent("key-1").state == "bound"


def test_crash_between_binder_and_mark_recovers(
    store: SessionControlStore,
) -> None:
    """binder 成功后、mark bound 前崩溃：同 claim 重试只产生一次绑定。"""
    intent = prepare_published_intent(store)
    # 模拟崩溃现场：claim 已提交、binder 已成功、mark bound 未发生。
    store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-a", claim_generation=1
    )
    assert store.get_initial_execution_intent("key-1").state == "pending"
    # 恢复 worker 使用同一 claim identity：幂等重入 → 单次有效绑定。
    worker = make_worker(store)
    worker.start()
    attempts = worker.bind_pending_once()
    assert [attempt.outcome for attempt in attempts] == ["bound"]
    bound = store.get_initial_execution_intent("key-1")
    assert bound.state == "bound"
    assert bound.execution_binding_id == intent.execution_binding_id
    assert bound.job_id == intent.job_id
    assert store.list_pending_initial_execution_intents() == ()


def test_two_workers_produce_single_binding(
    store: SessionControlStore,
) -> None:
    """两个 worker 并发消费同一 intent → 只产生一次有效绑定。"""
    prepare_published_intent(store)
    # 两个 worker = 同一库文件上的两个独立连接（真实并发形态；
    # BEGIN IMMEDIATE + busy_timeout 串行化 claim）。
    store_b = SessionControlStore(store.database_path)
    try:
        worker_a = make_worker(store, claim_owner="worker-a")
        worker_b = make_worker(store_b, claim_owner="worker-b")
        worker_a.start()
        worker_b.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda worker: worker.bind_pending_once(),
                    [worker_a, worker_b],
                )
            )
        outcomes = [
            attempt.outcome for attempts in results for attempt in attempts
        ]
        # 恰好一次 bound；输家按并发契约显式分类，不产生第二次绑定。
        assert outcomes.count("bound") == 1
        assert set(outcomes) <= {
            "bound",
            "skipped_claim_held",
            "skipped_already_bound",
        }
        bound = store.get_initial_execution_intent("key-1")
        assert bound.state == "bound"
        assert store.list_pending_initial_execution_intents() == ()
    finally:
        store_b.close()


def test_claim_conflict_is_skipped_without_binding(
    store: SessionControlStore,
) -> None:
    """intent 被其他 claim 持有 → 输家跳过，不产生绑定、不显式报错。"""
    prepare_published_intent(store)
    store.claim_initial_execution_intent(
        "key-1", claim_owner="worker-b", claim_generation=1
    )
    worker = make_worker(store, claim_owner="worker-a")
    worker.start()
    attempts = worker.bind_pending_once()
    assert [attempt.outcome for attempt in attempts] == ["skipped_claim_held"]
    intent = store.get_initial_execution_intent("key-1")
    assert intent.state == "pending"
    assert intent.claim_owner == "worker-b"


def test_binder_identity_drift_rejected(store: SessionControlStore) -> None:
    """binder 返回漂移 identity → 记录 last_error 并显式抛出，不 bound。"""
    prepare_published_intent(store)

    def drifting_binder(
        target: InitialExecutionBindingTarget,
    ) -> InitialExecutionBindOutcome:
        return InitialExecutionBindOutcome(
            execution_binding_id="tbind_" + "0" * 32,
            job_id=target.job_id,
        )

    worker = make_worker(store, binder=drifting_binder)
    worker.start()
    with pytest.raises(InitialExecutionBindingError, match="漂移"):
        worker.bind_pending_once()
    intent = store.get_initial_execution_intent("key-1")
    assert intent.state == "pending"
    assert "漂移" in str(intent.last_error)


def test_no_binder_start_reports_unavailable(
    store: SessionControlStore,
) -> None:
    """缺 binder：start 明确 unavailable，不把任何 intent 标成 bound。"""
    intent = prepare_published_intent(store)
    worker = InitialExecutionBindingWorker(
        store=store,
        claim_owner="worker-a",
        claim_generation=1,
        binder=None,
    )
    assert worker.binder_available is False
    with pytest.raises(RuntimeError, match="unavailable"):
        worker.start()
    # 未启动 → 消费入口拒绝。
    with pytest.raises(RuntimeError, match="未启动"):
        worker.bind_pending_once()
    # 状态零变更：intent 保持 pending、claim 为空。
    current = store.get_initial_execution_intent("key-1")
    assert current.state == "pending"
    assert current.claim_owner is None
    assert current.execution_binding_id == intent.execution_binding_id


def test_lifecycle_requires_start_and_stop_is_idempotent(
    store: SessionControlStore,
) -> None:
    """生命周期：start 前拒绝消费；重复 start 拒绝；stop 幂等。"""
    prepare_published_intent(store)
    worker = make_worker(store)
    with pytest.raises(RuntimeError, match="未启动"):
        worker.bind_pending_once()
    worker.start()
    with pytest.raises(RuntimeError, match="已启动"):
        worker.start()
    worker.stop()
    worker.stop()
    with pytest.raises(RuntimeError, match="未启动"):
        worker.bind_pending_once()
