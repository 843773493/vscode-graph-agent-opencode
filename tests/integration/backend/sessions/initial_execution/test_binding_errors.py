"""真实创建和准入后的绑定故障，不应把原生位置写入 intent 或汇总错误。"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing

import pytest

from app.agents.graph_binding import compute_capability_profile_hash
from app.container import AppContainer
from app.core.session_control_store import SessionControlStore, ThreadExecutionIntent
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBinder,
    InitialExecutionBindingAttempt,
    InitialExecutionBindingError,
    InitialExecutionBindingTarget,
    InitialExecutionBindingWorker,
    InitialExecutionBindOutcome,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)

_PRIVATE = "/private/initial-binding-source"
_WINDOWS = r"C:\private\initial-binding-target"


@pytest.fixture
async def published_intent(startup_container: AppContainer) -> AsyncIterator[tuple[SessionControlStore, ThreadExecutionIntent]]:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="真实初始绑定故障"))
    with container.thread_initial_execution_service._factory.for_owner_session(session.session_id) as creation:
        profile = {"thread_role": "child", "goal_enabled": False}
        created = await creation.create(
            idempotency_key="initial-binding-error", session_id=session.session_id, thread_id=None,
            delegation_id=None, initial_state="running", artifact_manifest={}, session_metadata={
                "graph_binding": {"graph_id": "deep-agent", "graph_revision": 1,
                    "graph_schema_hash": "sha256:" + "a" * 64,
                    "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile)},
                "capability_profile": profile,
                "task_seed": {"description": "验证绑定故障恢复", "agent_id": session.current_agent_id,
                    "gateway_id": None, "request_id": None},
                "task_reference": {"parent_session_id": session.session_id,
                    "parent_thread_id": await container.session_service.resolve_main_thread(session.session_id)},
            },
        )
        intent, = creation.control_store.list_pending_initial_execution_intents()
        assert intent.thread_id == created.child_thread_id and intent.state == "pending"
        assert intent.claim_owner is None and intent.last_error is None
        yield creation.control_store, intent


@pytest.fixture(params=["runtime", "os", "sqlite", "identity", "type"])
def binding_failure(request: pytest.FixtureRequest) -> Exception | InitialExecutionBindOutcome | dict[str, str]:
    if request.param == "runtime":
        error = RuntimeError(f"注入绑定错误: {_PRIVATE}")
        error.__cause__ = OSError(5, "底层异常链", _WINDOWS)
        return error
    if request.param == "os":
        return PermissionError(13, "注入绑定错误", _PRIVATE, None, _WINDOWS)
    if request.param == "sqlite":
        with closing(sqlite3.connect(":memory:")) as connection, pytest.raises(sqlite3.OperationalError) as caught:
            connection.execute(f'SELECT * FROM "{_PRIVATE}"')
        return caught.value
    if request.param == "identity":
        return InitialExecutionBindOutcome(execution_binding_id=_PRIVATE, job_id=_WINDOWS)
    return {"execution_binding_id": _PRIVATE, "job_id": _WINDOWS}


@pytest.mark.parametrize("after_admission", [False, True])
async def test_binding_error_preserves_diagnostics_and_recovers_same_execution(
    startup_container: AppContainer,
    published_intent: tuple[SessionControlStore, ThreadExecutionIntent],
    binding_failure: Exception | InitialExecutionBindOutcome | dict[str, str],
    caplog: pytest.LogCaptureFixture, after_admission: bool,
) -> None:
    container = startup_container
    store, original = published_intent
    real_binder = container.thread_initial_execution_service.binder

    def fail(target: InitialExecutionBindingTarget) -> InitialExecutionBindOutcome:
        if after_admission:
            real_binder(target)
        if isinstance(binding_failure, Exception):
            raise binding_failure
        return binding_failure  # type: ignore[return-value] —— 故意验证非法 binder 返回

    def consume(
        control: SessionControlStore, binder: InitialExecutionBinder, generation: int,
    ) -> tuple[InitialExecutionBindingAttempt, ...]:
        worker = InitialExecutionBindingWorker(
            store=control, claim_owner="initial-execution", claim_generation=generation, binder=binder,
        )
        worker.start()
        try:
            return worker.bind_pending_once()
        finally:
            worker.stop()

    async with container.session_service.session_write_admission(original.session_id):
        with pytest.raises(InitialExecutionBindingError) as caught:
            consume(store, fail, 1)
    failed = store.get_initial_execution_intent(original.admission_idempotency_key)
    assert failed.state == "pending" and failed.claim_generation == 1
    assert failed.claim_owner == "initial-execution"
    detail = str(failed.last_error)
    assert detail in str(caught.value)
    for location in (_PRIVATE, _WINDOWS, str(container.workspace_root), str(store.database_path)):
        assert location not in detail and location not in str(caught.value)
    for identity in (original.session_id, original.thread_id, original.execution_binding_id, original.job_id):
        assert identity in detail
    assert "stage=initial_execution_binding" in detail
    with closing(sqlite3.connect(store.database_path)) as connection:
        durable = connection.execute(
            "SELECT last_error FROM thread_execution_intents WHERE admission_idempotency_key=?",
            (original.admission_idempotency_key,),
        ).fetchone()[0]
    assert durable == detail
    diagnostic, = [record for record in caplog.records
                  if record.name.endswith("initial_execution_binding_worker")]
    if isinstance(binding_failure, Exception):
        assert f"error={type(binding_failure).__name__}" in detail
        assert diagnostic.exc_info and diagnostic.exc_info[1] is binding_failure
        assert str(binding_failure) in caplog.text
        if binding_failure.__cause__ is not None:
            assert str(binding_failure.__cause__) in caplog.text
        for name in ("errno", "winerror", "sqlite_errorcode"):
            code = getattr(binding_failure, name, None)
            if isinstance(code, int):
                assert f"{name}={code}" in detail
    else:
        assert diagnostic.exc_info is None and _PRIVATE in diagnostic.getMessage()
        assert repr(_WINDOWS)[1:-1] in diagnostic.getMessage()
        assert ("fields=execution_binding_id,job_id" if isinstance(binding_failure, InitialExecutionBindOutcome)
                else "返回类型非法: dict") in detail

    saver = container.checkpointer
    if not after_admission:
        with pytest.raises(KeyError) as missing:
            saver.execution_admission(original.session_id, thread_id=original.thread_id, execution_id=original.execution_binding_id)
        assert missing.value.args == (
            f"rollout 尚未物化: session_id={original.session_id}, thread_id={original.thread_id}",
        )
    else:
        assert saver.execution_admission(
            original.session_id, thread_id=original.thread_id, execution_id=original.execution_binding_id,
        ).job_id == original.job_id
    # 控制库冷重开后接管原 claim；真实 binder 必须复用已有 admission，不另造执行。
    with closing(SessionControlStore(store.database_path)) as cold:
        persisted = cold.get_initial_execution_intent(original.admission_idempotency_key)
        assert persisted.last_error == detail and persisted.state == "pending"
        async with container.session_service.session_write_admission(original.session_id):
            attempts = consume(cold, real_binder, 2)
            assert consume(cold, real_binder, 2) == ()
        assert len(attempts) == 1 and attempts[0].outcome == "bound"
        recovered = cold.get_initial_execution_intent(original.admission_idempotency_key)
        assert recovered.state == "bound" and recovered.last_error is None
        assert recovered.claim_generation == 2
        assert (recovered.execution_binding_id, recovered.job_id) == (original.execution_binding_id, original.job_id)
        assert len(cold.list_initial_execution_intents()) == 1
    admission = saver.execution_admission(original.session_id, thread_id=original.thread_id, execution_id=original.execution_binding_id)
    assert admission.job_id == original.job_id
    with saver._storage.storage.open_read_snapshot(original.session_id, original.thread_id) as snapshot:
        rows = snapshot.connection.execute(
            "SELECT execution_id,job_id FROM executions WHERE session_id=? AND thread_id=?",
            (original.session_id, original.thread_id),
        ).fetchall()
        assert [tuple(row) for row in rows] == [(original.execution_binding_id, original.job_id)]
    seed = saver.internal_seed_source(original.session_id, original.thread_id, original.execution_binding_id)
    assert seed is not None
