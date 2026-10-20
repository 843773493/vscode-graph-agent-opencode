"""冷观察、分页恢复、冻结 selector/deadline 与生命周期回调边界。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.session_control_communication_ledger.observations import (
    RemoteObservationError,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.core.trace_middleware import (
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.schemas.internal_v2.communication import CommunicationObservationRequest
from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextReadRequest,
)
from app.services.business.communication.addresses import (
    GlobalThreadAddress,
    ResolvedMainTargetBinding,
)
from app.services.business.communication.wait import WaitSelector
from app.services.business.session_context_projection import SessionContextReadSnapshot
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.services.orchestration.communication import observation_service
from app.services.orchestration.communication.service import SessionCommunicationService
from tests.support.communication import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    TARGET_SESSION,
    TARGET_THREAD,
    WORKSPACE_ID,
)
from tests.support.graph_binding import TEST_THREAD_GRAPH_BINDING

GATEWAY = "gateway_99f502c636a98dbcc80959cefae48c16"
SOURCE = GlobalThreadAddress(GATEWAY, WORKSPACE_ID, SOURCE_SESSION, SOURCE_THREAD)
TARGET = GlobalThreadAddress(GATEWAY, WORKSPACE_ID, TARGET_SESSION, TARGET_THREAD)


@dataclass
class _Clock:
    now: datetime = datetime(2026, 10, 7, tzinfo=UTC)
    ns: int = 0
    origin_id: str = "observation-boot-1"
    real_time_start: float | None = None

    def utc_now(self) -> datetime:
        return self.now if self.real_time_start is None else self.now + timedelta(seconds=time.monotonic() - self.real_time_start)

    def monotonic_ns(self) -> int:
        return self.ns


class _Paths:
    def __init__(self, root: Path) -> None:
        self.sessions_root = root

    def resolve_session_node(self, session_id: str) -> Path:
        assert session_id in {SOURCE_SESSION, TARGET_SESSION}
        return self.sessions_root / session_id

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        assert (session_id, thread_id) in {
            (SOURCE_SESSION, SOURCE_THREAD),
            (TARGET_SESSION, TARGET_THREAD),
        }
        return self.resolve_session_node(session_id)


class _Sessions:
    workspace_id = WORKSPACE_ID

    def __init__(self, root: Path) -> None:
        self.path_resolver = _Paths(root)
        self.gate = SessionLifecycleGate(root)
        self.main = TARGET_THREAD
        self.target_writes = 0
        self.before_target_write: Callable[[int], Awaitable[None]] | None = None

    async def resolve_main_thread(self, session_id: str) -> str:
        assert session_id == TARGET_SESSION
        return self.main

    def assert_session_active(self, session_id: str) -> None:
        assert session_id in {SOURCE_SESSION, TARGET_SESSION}

    @asynccontextmanager
    async def session_write_admission(self, session_id: str) -> AsyncIterator[None]:
        async with self.gate.exclusive(session_id):
            if session_id == TARGET_SESSION:
                self.target_writes += 1
                if self.before_target_write is not None:
                    await self.before_target_write(self.target_writes)
            yield


class _Saver:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []
        self.calls = 0

    def cold_execution_observations(
        self, session_id, thread_id, *, job_ids=None, turn_id=None
    ):
        assert (session_id, thread_id) == (TARGET_SESSION, TARGET_THREAD)
        self.calls += 1
        rows = [
            dict(row)
            for row in self.rows
            if (job_ids is None or row["job_id"] in job_ids)
            and (turn_id is None or row["turn_id"] == turn_id)
        ]
        return rows[:1] if turn_id is not None else rows


class _Query:
    def __init__(self) -> None:
        self.texts = ["第一页", "第二页", "第三页"]
        self.calls = 0

    async def freeze_read_context(
        self, request: SessionContextReadRequest
    ) -> SessionContextReadSnapshot:
        self.calls += 1
        return SessionContextReadSnapshot(
            revision="frozen-revision",
            items=[
                SessionContextItemDTO(kind="message", text=text, record_index=index)
                for index, text in enumerate(self.texts)
            ],
        )


class _RuntimeForbidden:
    async def submit(self, *args, **kwargs):
        raise AssertionError("read/wait 不得唤醒 runtime 或准入 Job")


class _Transport:
    def __init__(self) -> None:
        self.owner = None
        self.resolves = 0
        self.requests: list[CommunicationObservationRequest] = []
        self.denied = False
        self.after_reply: (
            Callable[[CommunicationObservationRequest], Awaitable[None]] | None
        ) = None

    async def resolve_main(self, **kwargs) -> ResolvedMainTargetBinding:
        self.resolves += 1
        # read discovery 保留规范 URI；wait 使用原 Session selector。
        assert (kwargs["session_id"], kwargs["workspace_id"]) in {
            (session_resource_uri(WORKSPACE_ID, TARGET_SESSION), WORKSPACE_ID),
            (TARGET_SESSION, None),
        }
        return ResolvedMainTargetBinding(TARGET_SESSION, TARGET_THREAD, TARGET)

    async def operation(self, *, operation, target, body, timeout=None):
        if self.denied:
            raise PermissionError("最新 policy 已撤权")
        request = CommunicationObservationRequest.model_validate(body)
        assert operation == request.operation and target == TARGET
        self.requests.append(request)
        result = await self.owner.observe(request)
        if self.after_reply is not None:
            await self.after_reply(request)
        return result


@dataclass
class _Harness:
    service: SessionCommunicationService
    sessions: _Sessions
    saver: _Saver
    query: _Query
    transport: _Transport
    clock: _Clock
    call_times: dict[str, datetime] = field(default_factory=dict)

    def store(self, session_id: str) -> SessionControlStore:
        return self.service._store(session_id)

    def restore(self) -> None:
        self.service = SessionCommunicationService(
            sessions=self.sessions,
            saver=self.saver,
            query_service=self.query,
            transport=self.transport,
            dispatcher=_RuntimeForbidden(),
        )
        self.service.observations._clock = self.clock
        self.transport.owner = self.service.observations

    async def read(self, call_id: str, request: SessionContextReadRequest):
        return await self.service.read(
            session_id=SOURCE_SESSION,
            thread_id=SOURCE_THREAD,
            source_call_id=call_id,
            source_call_created_at=self.call_times.setdefault(call_id, self.clock.utc_now()),
            request=request,
        )

    async def wait(
        self,
        call_id: str = "cop_wait",
        selector: WaitSelector | None = None,
        until="terminal",
        timeout=1,
    ):
        return await self.service.wait(
            session_id=SOURCE_SESSION,
            thread_id=SOURCE_THREAD,
            source_call_id=call_id,
            source_call_created_at=self.call_times.setdefault(call_id, self.clock.utc_now()),
            target_session_id=TARGET_SESSION,
            selector=selector,
            until=until,
            timeout_seconds=timeout,
        )

    async def frozen_wait(self) -> CommunicationObservationRequest:
        async with asyncio.timeout(2):
            while True:
                if self.transport.requests:
                    request = self.transport.requests[-1]
                    with closing(self.store(TARGET_SESSION)) as store:
                        observation = store.get_remote_observation(
                            request.observation_id, side="target"
                        )
                        if observation is not None and observation.snapshot is not None:
                            return request
                await asyncio.sleep(0)


@pytest.fixture
def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    sessions = _Sessions(tmp_path / "sessions")
    for session, thread in (
        (SOURCE_SESSION, SOURCE_THREAD),
        (TARGET_SESSION, TARGET_THREAD),
    ):
        node = sessions.path_resolver.resolve_session_node(session)
        node.mkdir(parents=True)
        with closing(SessionControlStore(node / CONTROL_DATABASE_NAME)) as store:
            store.initialize_main_thread(
                thread, datetime(2026, 10, 7, tzinfo=UTC), TEST_THREAD_GRAPH_BINDING
            )
            store.initialize_fence("active", 1)
    saver, query, transport, clock = _Saver(), _Query(), _Transport(), _Clock()
    service = SessionCommunicationService(
        sessions=sessions,
        saver=saver,
        query_service=query,
        transport=transport,
        dispatcher=_RuntimeForbidden(),
    )
    service.observations._clock = clock
    transport.owner = service.observations
    token = set_current_gateway_id(GATEWAY)
    yield _Harness(service, sessions, saver, query, transport, clock)
    reset_current_gateway_id(token)


@pytest.fixture
def read_request() -> SessionContextReadRequest:
    return SessionContextReadRequest(
        resource_identity=session_resource_identity(WORKSPACE_ID, TARGET_SESSION),
        uri=session_resource_uri(WORKSPACE_ID, TARGET_SESSION),
        scope="workspace",
        view="messages",
        limit=1,
    )


def _row(
    job="job_original",
    state="running",
    execution="execution_original",
    turn="turn_original",
) -> dict[str, str]:
    return {
        "job_id": job,
        "state": state,
        "execution_id": execution,
        "revision": job + ":" + state,
        "turn_id": turn,
    }


async def test_read_pages_restore_same_snapshot_without_runtime(
    harness: _Harness, read_request: SessionContextReadRequest
) -> None:
    first = await harness.read("cop_page_0", read_request)
    assert first.items[0].text == "第一页" and first.next_cursor.startswith("rc1.")
    harness.query.texts = ["稍后修改的内容"]
    harness.restore()
    second = await harness.read(
        "cop_page_1", read_request.model_copy(update={"cursor": first.next_cursor})
    )
    assert second.items[0].text == "第二页" and second.revision == first.revision
    assert harness.query.calls == harness.transport.resolves == 1
    assert not harness.service._started and harness.saver.calls == 0


async def test_replayed_page_rechecks_current_policy(
    harness: _Harness, read_request: SessionContextReadRequest
) -> None:
    first = await harness.read("cop_page_0", read_request)
    assert await harness.read("cop_page_0", read_request) == first
    harness.transport.denied = True
    with pytest.raises(PermissionError, match="撤权"):
        await harness.read("cop_page_0", read_request)
    assert harness.query.calls == 1


@pytest.mark.parametrize(
    "change", [{"view": "overview"}, {"limit": 2}, {"max_chars": 4096}]
)
async def test_cursor_rejects_changed_frozen_read_parameters(
    harness: _Harness,
    read_request: SessionContextReadRequest,
    change: dict[str, object],
) -> None:
    first = await harness.read("cop_page_0", read_request)
    with pytest.raises(ValueError, match="read-snapshot-mismatch"):
        await harness.read(
            "cop_page_1",
            read_request.model_copy(update={**change, "cursor": first.next_cursor}),
        )
    assert len(harness.transport.requests) == 1


async def test_cursor_authentication_rejects_mutation(
    harness: _Harness, read_request: SessionContextReadRequest
) -> None:
    first = await harness.read("cop_page_0", read_request)
    cursor = first.next_cursor
    changed = cursor[:8] + ("A" if cursor[8] != "A" else "B") + cursor[9:]
    with pytest.raises(ValueError, match="read-snapshot-invalid"):
        await harness.read(
            "cop_page_1", read_request.model_copy(update={"cursor": changed})
        )
    assert len(harness.transport.requests) == 1


async def test_frozen_target_main_cannot_follow_new_pointer(
    harness: _Harness, read_request: SessionContextReadRequest
) -> None:
    first = await harness.read("cop_page_0", read_request)
    harness.sessions.main = SOURCE_THREAD
    with pytest.raises(ValueError, match="read-snapshot-mismatch"):
        await harness.read(
            "cop_page_1", read_request.model_copy(update={"cursor": first.next_cursor})
        )
    assert harness.transport.resolves == 1


async def test_expired_cursor_does_not_open_new_observation(
    harness: _Harness, read_request: SessionContextReadRequest
) -> None:
    first = await harness.read("cop_page_0", read_request)
    harness.clock.now += timedelta(days=2)
    with pytest.raises(ValueError, match="read-snapshot-expired"):
        await harness.read(
            "cop_page_1", read_request.model_copy(update={"cursor": first.next_cursor})
        )
    assert harness.transport.resolves == 1 and len(harness.transport.requests) == 1


async def test_source_callback_keeps_original_lease_token(
    harness: _Harness, read_request: SessionContextReadRequest
) -> None:
    async def takeover(request: CommunicationObservationRequest) -> None:
        async with harness.sessions.session_write_admission(SOURCE_SESSION):
            with closing(harness.store(SOURCE_SESSION)) as store:
                lease = store.find_lease_by_operation(
                    "source-page|" + request.page_identity
                )
                store.takeover_lease(
                    lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
                )

    harness.transport.after_reply = takeover
    with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
        await harness.read("cop_page_0", read_request)
    request = harness.transport.requests[0]
    with closing(harness.store(SOURCE_SESSION)) as store:
        assert (
            store.get_remote_page(
                request.observation_id,
                side="source",
                page_identity=request.page_identity,
            ).result
            is None
        )


async def test_target_snapshot_commit_keeps_original_lease_token(
    harness: _Harness, read_request: SessionContextReadRequest
) -> None:
    async def takeover(count: int) -> None:
        if count == 2:
            request = harness.transport.requests[0]
            with closing(harness.store(TARGET_SESSION)) as store:
                lease = store.find_lease_by_operation(
                    "target-observation|" + request.observation_id
                )
                store.takeover_lease(
                    lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
                )

    harness.sessions.before_target_write = takeover
    with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
        await harness.read("cop_page_0", read_request)
    with closing(harness.store(TARGET_SESSION)) as store:
        assert (
            store.get_remote_observation(
                harness.transport.requests[0].observation_id, side="target"
            ).snapshot
            is None
        )


async def test_idle_wait_does_not_subscribe_future_job(harness: _Harness) -> None:
    first = await harness.wait()
    assert first["status"] == "idle" and first["observed"] == []
    harness.saver.rows = [_row()]
    assert await harness.wait() == first


@pytest.mark.parametrize(
    "selector",
    [
        WaitSelector("job", "job_missing"),
        WaitSelector("turn", "turn_missing"),
        WaitSelector("communication", "comm_missing"),
    ],
)
async def test_missing_explicit_selector_never_returns_idle(
    harness: _Harness, selector: WaitSelector
) -> None:
    call_id = "cop_missing_explicit_selector"
    with pytest.raises(RemoteObservationError) as first:
        await harness.wait(call_id=call_id, selector=selector)
    assert first.value.code == "selector_not_found"

    request = harness.transport.requests[-1]
    expected = {
        "error": {
            "code": "selector_not_found",
            "message": "selector_not_found",
            "detail": {},
        }
    }
    with closing(harness.store(TARGET_SESSION)) as store:
        assert store.get_remote_observation(
            request.observation_id, side="target"
        ).result == expected
        assert store.get_remote_page(
            request.observation_id,
            side="target",
            page_identity=request.page_identity,
        ).result == expected

    with pytest.raises(RemoteObservationError) as replay:
        await harness.wait(call_id=call_id, selector=selector)
    assert replay.value.code == "selector_not_found"
    assert len(harness.transport.requests) == 2
    assert harness.transport.requests[-1].terminal_replay is True
    assert harness.transport.requests[-1].page_identity == request.page_identity


async def test_wait_freezes_jobs_and_ignores_later_admission(harness: _Harness) -> None:
    harness.saver.rows = [_row()]
    pending = asyncio.create_task(harness.wait())
    await harness.frozen_wait()
    harness.saver.rows = [
        _row(state="completed"),
        _row(job="job_future", execution="execution_future"),
    ]
    await harness.service.observations.notify()
    result = await asyncio.wait_for(pending, 2)
    assert result["status"] == "completed"
    assert [item["selector_id"] for item in result["observed"]] == ["job_original"]


async def test_wait_detects_execution_identity_replacement(harness: _Harness) -> None:
    harness.saver.rows = [_row()]
    pending = asyncio.create_task(
        harness.wait(selector=WaitSelector("job", "job_original"))
    )
    await harness.frozen_wait()
    harness.saver.rows = [_row(state="completed", execution="different_execution")]
    await harness.service.observations.notify()
    with pytest.raises(RuntimeError, match="没有唯一 execution"):
        await asyncio.wait_for(pending, 2)


async def test_pending_communication_observes_binding_notification(
    harness: _Harness,
) -> None:
    communication_id = "comm_" + "1" * 32
    with closing(harness.store(TARGET_SESSION)) as store:
        store.create_or_get_communication_inbox(
            session_id=TARGET_SESSION,
            communication_id=communication_id,
            source_gateway_id=GATEWAY,
            source_workspace_id=WORKSPACE_ID,
            source_session_id=SOURCE_SESSION,
            source_thread_id=SOURCE_THREAD,
            target_gateway_id=GATEWAY,
            target_workspace_id=WORKSPACE_ID,
            target_thread_id=TARGET_THREAD,
            kind="result",
            reply_to_communication_id=None,
            payload_hash="1" * 64,
            content="只观察",
            delivery_policy="after_turn",
            request_id="req_unit",
        )
    pending = asyncio.create_task(
        harness.wait(selector=WaitSelector("communication", communication_id))
    )
    await harness.frozen_wait()
    harness.saver.rows = [_row(state="completed")]
    with closing(harness.store(TARGET_SESSION)) as store:
        store.claim_communication_inbox_admission(
            communication_id, claim_owner="test_binder", claim_generation=1
        )
        store.mark_communication_inbox_execution_bound(
            communication_id,
            job_id="job_original",
            execution_id="execution_original",
            turn_id=None,
            claim_owner="test_binder",
            claim_generation=1,
        )
    await harness.service.observations.notify()
    result = await asyncio.wait_for(pending, 2)
    assert (
        result["status"] == "completed"
        and result["observed"][0]["selector_kind"] == "communication"
    )


async def test_turn_selector_observes_latest_attempt(harness: _Harness) -> None:
    harness.saver.rows = [
        _row(job="job_second", state="cancelled", execution="execution_second"),
        _row(state="completed"),
    ]
    result = await harness.wait(selector=WaitSelector("turn", "turn_original"))
    assert result["status"] == "cancelled" and len(result["observed"]) == 1
    assert result["observed"] == [{
        "selector_kind": "turn", "selector_id": "turn_original",
        "state": "cancelled", "revision": "job_second:cancelled",
    }]
    request = harness.transport.requests[-1]
    with closing(harness.store(TARGET_SESSION)) as store:
        observation = store.get_remote_observation(request.observation_id, side="target")
        assert observation.snapshot["identities"] == [{
            "kind": "turn", "id": "turn_original",
            "job_id": "job_second", "execution_id": "execution_second",
        }]


@pytest.mark.parametrize("rollback", [False, True])
async def test_restart_keeps_deadline_and_clock_rollback_expires(
    harness: _Harness, rollback: bool
) -> None:
    harness.saver.rows = [_row()]
    pending = asyncio.create_task(harness.wait())
    before = await harness.frozen_wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    harness.clock.origin_id = "observation-boot-2"
    harness.clock.now += timedelta(seconds=-1 if rollback else 0.99)
    if not rollback:
        harness.clock.real_time_start = time.monotonic()
    harness.restore()
    result = await asyncio.wait_for(harness.wait(), 2)
    assert result["status"] == "timed_out"
    assert harness.transport.requests[-1].query["deadline"] == before.query["deadline"]
    assert harness.transport.resolves == 1


async def test_notification_before_subscription_and_deadline_terminal_race(
    harness: _Harness,
) -> None:
    harness.saver.rows = [_row()]

    async def complete_before_subscribe(count: int) -> None:
        if count == 2:
            harness.saver.rows = [_row(state="completed")]
            harness.clock.ns = 1_000_000_000
            await harness.service.observations.notify()

    harness.sessions.before_target_write = complete_before_subscribe
    result = await asyncio.wait_for(harness.wait(), 2)
    assert result["status"] == "completed"
    assert result["baseline_revision"] != result["latest_revision"]


async def test_state_change_wait_reuses_original_baseline(harness: _Harness) -> None:
    harness.saver.rows = [_row(state="pending")]
    pending = asyncio.create_task(harness.wait(until="state_change"))
    await harness.frozen_wait()
    harness.saver.rows = [_row(state="running")]
    await harness.service.observations.notify()
    result = await asyncio.wait_for(pending, 2)
    assert (
        result["status"] == "running"
        and result["baseline_revision"] != result["latest_revision"]
    )


@pytest.mark.parametrize("field", ["t", "r", "u", "p", "b", "k", "n", "c", "e"])
async def test_authenticated_cursor_binds_all_frozen_fields(harness: _Harness, read_request: SessionContextReadRequest, field: str) -> None:
    owner = harness.service.observations
    first = await harness.read("cop_binding_0", read_request)
    with closing(harness.store(SOURCE_SESSION)) as store:
        binding = owner._decode_token(store, SOURCE, first.next_cursor, prefix="rc1.")
        assert set(binding) == {"o", "t", "r", "u", "p", "b", "k", "n", "c", "e", "f"}
        if field == "t":
            binding[field] = {**binding[field], "thread_id": SOURCE_THREAD}
        elif field == "u":
            binding[field] = [99, *binding[field][1:]]
        elif field == "b":
            binding[field] = [99, binding[field][1]]
        elif field in {"n", "c"}:
            binding[field] += 1
        elif field == "e":
            binding[field] = (harness.clock.now + timedelta(days=3)).isoformat()
        else:
            binding[field] = "changed-frozen-value"
        cursor = owner._encode_token(store, SOURCE, binding, prefix="rc1.")
    with pytest.raises(ValueError, match="read-snapshot-mismatch"):
        await harness.read("cop_binding_1", read_request.model_copy(update={"cursor": cursor}))
    assert len(harness.transport.requests) == 1


async def test_expired_operations_remove_every_payload_page_and_terminal_lease(harness: _Harness) -> None:
    for index in range(16):
        assert (await harness.wait(call_id=f"cop_expiry_{index}"))["status"] == "idle"
    harness.clock.now += timedelta(days=2)
    for session_id in (SOURCE_SESSION, TARGET_SESSION):
        async with harness.sessions.session_write_admission(session_id):
            with closing(harness.store(session_id)) as store:
                harness.service.observations._collect_observations(store)
                assert store.list_remote_observations() == ()
                assert store.connection.execute("SELECT count(*) FROM remote_observation_pages").fetchone()[0] == 0
                assert store.connection.execute("SELECT count(*) FROM session_operation_leases WHERE operation_kind='remote_observation'").fetchone()[0] == 0
    with pytest.raises(ValueError, match="operation-retry-expired"):
        await harness.wait(call_id="cop_expiry_0")
    assert harness.transport.resolves == 16


async def test_live_wait_expiry_preserves_holder_until_original_result(harness: _Harness) -> None:
    """UTC 重试窗口关闭不能改写仍有效的 monotonic wait 或任一 holder token。"""
    owner = harness.service.observations
    harness.saver.rows = [_row()]
    pending = asyncio.create_task(harness.wait(call_id="cop_live_expiry", timeout=30))
    request = await harness.frozen_wait()
    before = {}
    for session_id in (SOURCE_SESSION, TARGET_SESSION):
        with closing(harness.store(session_id)) as store:
            before[session_id] = store.list_non_terminal_leases()
    harness.clock.now += timedelta(days=2)
    try:
        for session_id, side in ((SOURCE_SESSION, "source"), (TARGET_SESSION, "target")):
            await owner._recover_observation(session_id, side, request.observation_id)
            async with harness.sessions.session_write_admission(session_id):
                with closing(harness.store(session_id)) as store:
                    assert owner._collect_observations(store) is None
                    assert store.list_non_terminal_leases() == before[session_id]
                    assert store.get_remote_observation(request.observation_id, side=side) is not None
        with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
            await harness.wait(call_id="cop_live_expiry", timeout=30)
        assert not pending.done()
        harness.saver.rows = [_row(state="completed")]
        await owner.notify()
        with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
            await asyncio.wait_for(pending, 2)
        for session_id, side in ((SOURCE_SESSION, "source"), (TARGET_SESSION, "target")):
            async with harness.sessions.session_write_admission(session_id):
                with closing(harness.store(session_id)) as store:
                    assert store.list_non_terminal_leases() == ()
                    root = store.find_lease_by_operation(side + "-observation|" + request.observation_id)
                    assert root.fencing_token == before[session_id][0].fencing_token
                    if side == "target":
                        assert store.get_remote_observation(request.observation_id, side=side).result["status"] == "completed"
                    owner._collect_observations(store)
                    assert store.list_remote_observations() == ()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cold_expired_wait_resumes_original_deadline_and_cancels_stopped_source(harness: _Harness) -> None:
    harness.saver.rows = [_row()]
    pending = asyncio.create_task(harness.wait(call_id="cop_cold_expiry", timeout=30))
    request = await harness.frozen_wait()
    with closing(harness.store(TARGET_SESSION)) as store:
        frozen = store.get_remote_observation(request.observation_id, side="target").snapshot
        old = store.find_lease_by_operation("target-observation|" + request.observation_id)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    harness.clock.now += timedelta(days=2)
    harness.clock.origin_id = "observation-cold-boot"
    harness.restore()
    owner = harness.service.observations
    await owner._recover_observation(TARGET_SESSION, "target", request.observation_id)
    with closing(harness.store(TARGET_SESSION)) as store:
        current = store.get_remote_observation(request.observation_id, side="target")
        assert current.snapshot == frozen
        assert current.request["query"]["deadline"] == request.query["deadline"]
        assert current.result["status"] == "timed_out"
        lease = store.find_lease_by_operation("target-observation|" + request.observation_id)
        assert lease.fencing_token == old.fencing_token + 1 and lease.state == "completed"
        assert not store.verify_lease_token(lease_id=old.lease_id, fencing_token=old.fencing_token)
    await owner._recover_observation(SOURCE_SESSION, "source", request.observation_id)
    with closing(harness.store(SOURCE_SESSION)) as store:
        page = store.get_remote_page(request.observation_id, side="source", page_identity=request.page_identity)
        assert page.result["cancellation"]["reason"] == "holder-stopped"
        assert page.result["error"]["code"] == "operation-retry-expired"
        assert store.list_non_terminal_leases() == ()
    for session_id in (SOURCE_SESSION, TARGET_SESSION):
        async with harness.sessions.session_write_admission(session_id):
            with closing(harness.store(session_id)) as store:
                owner._collect_observations(store)
                assert store.list_remote_observations() == ()
    with closing(harness.store(TARGET_SESSION)) as store:
        with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
            observation_service._assert_current(store, old)
        assert store.list_remote_observations() == ()
        assert store.connection.execute("SELECT count(*) FROM session_operation_leases WHERE operation_kind='remote_observation'").fetchone()[0] == 0
    with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
        await harness.wait(call_id="cop_cold_expiry", timeout=30)
    assert len(harness.transport.requests) == 1


@pytest.mark.parametrize("boundary", ["source-page|", "source-observation|", "target-page|", "target-observation|"])
async def test_commit_interruption_replays_result_before_settling(
    harness: _Harness, read_request: SessionContextReadRequest,
    monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    """真实提交结果后中断，原 identity 重放只补 lease 终结，不重取快照。"""
    original = observation_service._settle

    def interrupt(store, lease, *, outcome="completed"):
        if lease.operation_identity.startswith(boundary):
            raise asyncio.CancelledError
        return original(store, lease, outcome=outcome)

    monkeypatch.setattr(observation_service, "_settle", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await harness.read("cop_commit_interrupt", read_request)
    request = harness.transport.requests[-1]
    monkeypatch.setattr(observation_service, "_settle", original)
    harness.query.texts = ["重启后不得重读"]
    harness.restore()
    result = await harness.read("cop_commit_interrupt", read_request)
    assert result.items[0].text == "第一页"
    assert harness.query.calls == harness.transport.resolves == 1
    assert harness.transport.requests[-1] == request.model_copy(update={
        "terminal_replay": boundary.startswith("source-"),
    })
    for session_id in (SOURCE_SESSION, TARGET_SESSION):
        with closing(harness.store(session_id)) as store:
            assert store.list_non_terminal_leases() == ()


async def test_cold_admitted_page_finishes_after_cursor_expiry(
    harness: _Harness, read_request: SessionContextReadRequest,
) -> None:
    first = await harness.read("cop_cold_page_0", read_request)
    owner = harness.service.observations
    before = harness.sessions.target_writes

    async def interrupt_before_page_result(count: int) -> None:
        if count == before + 2:
            raise asyncio.CancelledError

    harness.sessions.before_target_write = interrupt_before_page_result
    with pytest.raises(asyncio.CancelledError):
        await harness.read("cop_cold_page_1", read_request.model_copy(update={"cursor": first.next_cursor}))
    request = harness.transport.requests[-1]
    with closing(harness.store(TARGET_SESSION)) as store:
        assert store.get_remote_page(request.observation_id, side="target", page_identity=request.page_identity).result is None
    harness.sessions.before_target_write = None
    harness.clock.now += timedelta(days=2)
    harness.restore()
    owner = harness.service.observations
    await owner._recover_observation(TARGET_SESSION, "target", request.observation_id)
    with closing(harness.store(TARGET_SESSION)) as store:
        page = store.get_remote_page(request.observation_id, side="target", page_identity=request.page_identity)
        assert page.result["result"]["items"][0]["text"] == "第二页"
        assert store.list_non_terminal_leases() == ()
    assert harness.query.calls == 1
    with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
        await owner.observe(request)


async def test_expired_cold_source_keeps_committed_result_before_settling(
    harness: _Harness, read_request: SessionContextReadRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = observation_service._settle

    def interrupt(store, lease, *, outcome="completed"):
        if lease.operation_identity.startswith("source-page|"):
            raise asyncio.CancelledError
        return original(store, lease, outcome=outcome)

    monkeypatch.setattr(observation_service, "_settle", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await harness.read("cop_committed_expiry", read_request)
    request = harness.transport.requests[-1]
    with closing(harness.store(SOURCE_SESSION)) as store:
        before = store.get_remote_page(request.observation_id, side="source", page_identity=request.page_identity)
        old = store.find_lease_by_operation("source-page|" + request.page_identity)
    monkeypatch.setattr(observation_service, "_settle", original)
    harness.clock.now += timedelta(days=2)
    harness.restore()
    await harness.service.observations._recover_observation(SOURCE_SESSION, "source", request.observation_id)
    with closing(harness.store(SOURCE_SESSION)) as store:
        assert store.get_remote_page(request.observation_id, side="source", page_identity=request.page_identity) == before
        current = store.get_lease(old.lease_id)
        assert current.state == "completed" and current.fencing_token == old.fencing_token + 1
        assert store.list_non_terminal_leases() == ()
        harness.service.observations._collect_observations(store)
        with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
            observation_service._assert_current(store, old)
    assert len(harness.transport.requests) == 1
