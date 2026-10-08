"""真实 catalog/删除 owner 与 HTTP 边界的观察生命周期集成验证。"""

from __future__ import annotations

import asyncio
import base64
import shutil
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.core.session_control_communication_ledger.observations import (
    RemoteObservationError,
)
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.communication import CommunicationObservationRequest
from app.schemas.internal_v2.session_context import SessionContextReadRequest
from app.services.business.communication.addresses import (
    GlobalThreadAddress,
    ResolvedMainTargetBinding,
)
from app.services.business.communication.wait import WaitSelector
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_communication_client import (
    GatewayCommunicationClient,
)
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.communication.observation_service import _page_identity
from app.services.orchestration.communication.service import SessionCommunicationService
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.support.api_helpers import create_session
from tests.support.gateway_processes import LOCAL_TOKEN_HEADERS
from tests.support.session_navigation_http import submit_navigation_operation
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)

pytest_plugins = ["tests.integration.backend.sessions.test_session_communication_pipeline"]

GATEWAY = "gateway_observation_lifecycle"


@dataclass
class _Clock:
    now: datetime
    origin_id: str = "lifecycle-test-boot"
    ns: int = 0

    def utc_now(self) -> datetime:
        return self.now

    def monotonic_ns(self) -> int:
        return self.ns


class _ColdProjection:
    """受控的冷投影输入；实际分页、冻结、目录和 operation owner 均为生产实现。"""

    def __init__(self) -> None:
        self.records = [{"role": "user", "content": text} for text in ("第一条", "第二条", "第三条")]
        self.executions: list[dict[str, str]] = []

    async def get_agent_context_state(self, session_id: str, thread_id: str) -> dict[str, object]:
        return {"records": self.records, "raw_message_count": len(self.records), "compacted": False, "compaction_cutoff": None}

    def cold_execution_observations(self, session_id: str, thread_id: str, *, job_ids=None, turn_id=None):
        return [dict(row) for row in self.executions if (job_ids is None or row["job_id"] in job_ids) and (turn_id is None or row["turn_id"] == turn_id)]

    def __getattr__(self, name: str):
        raise AssertionError("read/wait 不得使用 runtime/assembly 写入: " + name)


class _Transport:
    def __init__(self, sessions: SessionService) -> None:
        self.sessions = sessions
        self.owner = None
        self.requests: list[CommunicationObservationRequest] = []
        self.lose_next_response = False
        self.denied = False

    async def resolve_main(self, *, source: GlobalThreadAddress, session_id: str, workspace_id: str | None) -> ResolvedMainTargetBinding:
        thread = await self.sessions.resolve_main_thread(session_id)
        target = GlobalThreadAddress(GATEWAY, self.sessions.workspace_id, session_id, thread)
        return ResolvedMainTargetBinding(session_id, thread, target)

    async def operation(self, *, operation: str, target: GlobalThreadAddress, body: dict[str, object], timeout=None):
        if self.denied:
            raise PermissionError("authorization_revoked")
        request = CommunicationObservationRequest.model_validate(body)
        self.requests.append(request)
        result = await self.owner.observe(request)
        if self.lose_next_response:
            self.lose_next_response = False
            raise ConnectionError("target 已提交，source 未保存")
        return result


@dataclass
class _Case:
    workspace: CatalogWorkspaceContext
    sessions: SessionService
    projection: _ColdProjection
    transport: _Transport
    clock: _Clock
    service: SessionCommunicationService
    source: str
    source_thread: str
    target: str
    target_thread: str
    tasks: list[asyncio.Task] = field(default_factory=list)
    call_times: dict[str, datetime] = field(default_factory=dict)

    def restore(self) -> None:
        query = SessionContextQueryService(message_source=self.projection, session_lookup=self.sessions, assembly_source=self.projection, workspace_id=self.workspace.workspace_id)
        self.service = SessionCommunicationService(sessions=self.sessions, saver=self.projection, dispatcher=self.projection, transport=self.transport, query_service=query)
        self.service.observations._clock = self.clock
        self.transport.owner = self.service.observations

    def read_request(self, **changes) -> SessionContextReadRequest:
        return SessionContextReadRequest(resource_identity=session_resource_identity(self.workspace.workspace_id, self.target), uri=session_resource_uri(self.workspace.workspace_id, self.target), scope="workspace", view="messages", limit=1, **changes)

    async def read(self, call: str, request: SessionContextReadRequest | None = None):
        return await self.service.read(session_id=self.source, thread_id=self.source_thread, source_call_id=call, source_call_created_at=self.call_times.setdefault(call, self.clock.now), request=request or self.read_request())

    async def delete_target(self) -> None:
        await self.workspace.resolver.delete_session_subtree(self.target)


@pytest.fixture
async def lifecycle_case() -> AsyncIterator[_Case]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    if context.workspace_root.exists():
        shutil.rmtree(context.workspace_root)
    workspace = build_catalog_workspace(context.output_root)
    sessions = SessionService(config_service=ConfigService(), trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root), workspace_id=workspace.workspace_id, path_resolver=workspace.resolver, creation_service=workspace.creation_service)
    source = await workspace.create_session("观察源")
    target = await workspace.create_session("观察目标")
    projection = _ColdProjection()
    transport = _Transport(sessions)
    case = _Case(workspace, sessions, projection, transport, _Clock(datetime.now(UTC)), None, source, workspace.resolver.main_thread_id(source), target, workspace.resolver.main_thread_id(target))
    case.restore()

    async def drain(session_id: str) -> None:
        await case.service.observations.drain_session(session_id)

    workspace.resolver.bind_session_drain_callback(drain)
    token = set_current_gateway_id(GATEWAY)
    try:
        yield case
    finally:
        for task in case.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*case.tasks, return_exceptions=True)
        reset_current_gateway_id(token)
        workspace.close()


async def test_real_catalog_response_loss_restores_same_page_and_ordinal(lifecycle_case: _Case) -> None:
    case = lifecycle_case
    case.transport.lose_next_response = True
    with pytest.raises(ConnectionError, match="已提交"):
        await case.read("first")
    first_request = case.transport.requests[-1]
    case.projection.records = [{"role": "user", "content": "后来变化"}]
    case.restore()
    first = await case.read("first")
    assert first.items[0].text == "第一条"
    second = await case.read("second", case.read_request(cursor=first.next_cursor))
    assert second.items[0].text == "第二条" and second.revision == first.revision
    assert case.transport.requests[-1].observation_id == first_request.observation_id
    assert case.transport.requests[-1].page_ordinal == 1
    with pytest.raises(RemoteObservationError, match="read-snapshot-mismatch"):
        await case.read("different-call-at-same-ordinal", case.read_request(cursor=first.next_cursor))
    with closing(case.service._store(case.source)) as store:
        pages = store.list_remote_pages(first_request.observation_id, side="source")
        assert len(pages) == 2 and [page.page_ordinal for page in pages] == [0, 1]


async def test_real_delete_preserves_original_page_rejects_new_call_and_collects(lifecycle_case: _Case) -> None:
    case = lifecycle_case
    case.transport.lose_next_response = True
    with pytest.raises(ConnectionError):
        await case.read("original")
    original = case.transport.requests[-1]
    await case.delete_target()
    with pytest.raises(KeyError):
        case.workspace.resolver.resolve_session_node(case.target)
    isolated = case.workspace.resolver.resolve_operation_replay_node(case.target, now=case.clock.now)
    before = (isolated / "session-control.sqlite").read_bytes()
    restored = await case.read("original")
    assert restored.items[0].text == "第一条"
    assert (isolated / "session-control.sqlite").read_bytes() == before
    with pytest.raises(RemoteObservationError, match="target-deleted"):
        await case.read("new-page", case.read_request(cursor=restored.next_cursor))
    case.transport.denied = True
    with pytest.raises(PermissionError, match="authorization_revoked"):
        await case.read("original")
    case.transport.denied = False
    case.clock.now += timedelta(days=2)
    await case.service.observations.collect_expired()
    assert not isolated.exists()
    with closing(case.service._store(case.source)) as store:
        assert store.list_remote_observations() == ()
        assert store.connection.execute("SELECT count(*) FROM remote_observation_pages").fetchone()[0] == 0
        assert store.connection.execute("SELECT count(*) FROM session_operation_leases WHERE operation_kind='remote_observation'").fetchone()[0] == 0
    with pytest.raises(RemoteObservationError, match="operation-retry-expired"):
        await case.read("original")
    with pytest.raises(RemoteObservationError, match="operation-retry-expired"):
        await case.service.observations.observe(original)


async def test_real_pending_wait_delete_terminalizes_before_isolation(lifecycle_case: _Case) -> None:
    case = lifecycle_case
    case.projection.executions = [{"job_id": "job_wait", "execution_id": "execution_wait", "turn_id": "turn_wait", "state": "running", "revision": "running"}]
    task = asyncio.create_task(case.service.wait(session_id=case.source, thread_id=case.source_thread, source_call_id="wait_original", source_call_created_at=case.clock.now, target_session_id=case.target, selector=WaitSelector("job", "job_wait"), until="terminal", timeout_seconds=30))
    case.tasks.append(task)
    async with asyncio.timeout(5):
        while True:
            if case.transport.requests:
                request = case.transport.requests[-1]
                with closing(case.service._store(case.target)) as store:
                    observation = store.get_remote_observation(request.observation_id, side="target")
                    if observation is not None and observation.snapshot is not None:
                        break
            await asyncio.sleep(0)
    await case.delete_target()
    with pytest.raises(RemoteObservationError, match="target-deleted"):
        await task
    isolated = case.workspace.resolver.resolve_operation_replay_node(case.target, now=case.clock.now)
    from app.core.session_control_store import SessionControlStore
    with closing(SessionControlStore(isolated / "session-control.sqlite", read_only=True)) as store:
        assert store.list_non_terminal_leases() == ()
        observation = store.get_remote_observation(request.observation_id, side="target")
        assert observation.snapshot["identities"] == [{"kind": "job", "id": "job_wait", "job_id": "job_wait", "execution_id": "execution_wait"}]
        assert observation.result["error"]["code"] == "target-deleted"
    with pytest.raises(RemoteObservationError, match="target-deleted"):
        await case.service.observations.observe(request)

async def test_real_http_cursor_errors_gateway_source_and_deleted_page(
    communication_case: CommunicationCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Workspace/Gateway HTTP；只用受控 Provider 写入目标可读记录。"""
    case = communication_case
    case.model.response_callback = lambda _payload: {"content": "冻结的目标回答"}
    source_id = await create_session(case.client, "HTTP observation source")
    target_id = await create_session(case.client, "HTTP observation target")
    seeded = await case.client.post(f"/api/v1/sessions/{target_id}/messages", json={
        "message": {"content": "目标原始问题"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert seeded.status_code == 200, seeded.text
    await _completed(case.client, seeded.json()["data"]["job_id"])
    source_thread = (await case.client.get(f"/api/v1/sessions/{source_id}/main-thread")).json()["data"]["thread_id"]
    target_thread = (await case.client.get(f"/api/v1/sessions/{target_id}/main-thread")).json()["data"]["thread_id"]
    source = GlobalThreadAddress(case.gateway_id, case.backend.workspace_id, source_id, source_thread)
    target = GlobalThreadAddress(case.gateway_id, case.backend.workspace_id, target_id, target_thread)
    read = SessionContextReadRequest(resource_identity=session_resource_identity(target.workspace_id, target.session_id), uri=session_resource_uri(target.workspace_id, target.session_id), scope="workspace", view="messages", limit=1)
    request = CommunicationObservationRequest(observation_id="obs_http_lifecycle", source_call_id="cop_http_first", page_identity=_page_identity(source, "cop_http_first"), page_ordinal=0, expires_at=datetime.now(UTC) + timedelta(days=1), source=source, target=target, operation="read", query={"read": read.model_dump(mode="json")})
    relay = {"operation": "read", "target": request.model_dump(mode="json")["target"], "body": request.model_dump(mode="json")}
    first_response = await case.client.post("/api/gateway/federation/relay", json=relay)
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()["data"]
    assert first["result"]["items"][0]["text"] == "目标原始问题"
    raw = bytearray(base64.urlsafe_b64decode(first["result"]["next_cursor"][4:]))
    raw[-1] ^= 1
    tampered = "rt1." + base64.urlsafe_b64encode(raw).decode()
    bad = request.model_copy(update={
        "source_call_id": "cop_http_bad", "page_identity": _page_identity(source, "cop_http_bad"),
        "page_ordinal": 1, "query": {"read": read.model_copy(update={"cursor": tampered}).model_dump(mode="json")},
    })
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{case.backend.port}", headers={**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Gateway-Id": case.gateway_id, "X-BoxTeam-Workspace-Id": case.backend.workspace_id, "X-BoxTeam-Communication-Origin": case.gateway_id}) as backend:
        direct = await backend.post(f"/api/v1/sessions/{target_id}/communication-observations", json=bad.model_dump(mode="json"))
    case.record("observation-direct-response", direct.json())
    assert direct.status_code == 409, direct.text
    assert direct.json()["detail"] == {"code": "read-snapshot-invalid", "message": "read-snapshot-invalid", "detail": {}}
    assert direct.headers["X-Request-ID"] == direct.json()["request_id"]
    relayed = await case.client.post("/api/gateway/federation/relay", json={**relay, "body": bad.model_dump(mode="json")})
    assert relayed.status_code == 409
    assert relayed.json()["detail"]["code"] == "read-snapshot-invalid"
    assert relayed.headers["X-Request-ID"] == relayed.json()["request_id"]
    monkeypatch.setattr("app.services.infrastructure.gateway_communication_client.get_gateway_local_token", lambda: case.client.headers["X-Local-Token"])
    source_client = GatewayCommunicationClient(gateway_url=f"http://127.0.0.1:{case.gateway.port}", timeout_seconds=10)
    token = set_current_request_id("req_source_cursor_error")
    try:
        with pytest.raises(RemoteObservationError, match="^read-snapshot-invalid$"):
            await source_client.operation(operation="read", target=target, body=bad.model_dump(mode="json"))
    finally:
        reset_current_request_id(token)
    mismatch = request.model_copy(update={"target": GlobalThreadAddress(target.gateway_id, target.workspace_id, target.session_id, source_thread)})
    mismatched = await case.client.post("/api/gateway/federation/relay", json={"operation": "read", "target": mismatch.model_dump(mode="json")["target"], "body": mismatch.model_dump(mode="json")})
    assert mismatched.status_code == 409 and mismatched.json()["detail"]["code"] == "read-snapshot-mismatch"
    expired = request.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    expired_response = await case.client.post("/api/gateway/federation/relay", json={**relay, "body": expired.model_dump(mode="json")})
    assert expired_response.status_code == 409 and expired_response.json()["detail"]["code"] == "operation-retry-expired"
    deletion = await submit_navigation_operation(case.client, "delete_session", target_node_id=target_id)
    assert deletion["state"] == "committed", deletion
    restored = await case.client.post("/api/gateway/federation/relay", json=relay)
    assert restored.status_code == 200 and restored.json()["data"] == first, restored.text
    new_page = bad.model_copy(update={"source_call_id": "cop_http_new_after_delete", "page_identity": _page_identity(source, "cop_http_new_after_delete"), "query": {"read": read.model_copy(update={"cursor": first["result"]["next_cursor"]}).model_dump(mode="json")}})
    rejected = await case.client.post("/api/gateway/federation/relay", json={**relay, "body": new_page.model_dump(mode="json")})
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "target-deleted"
    case.record("observation-http-errors", {"direct": direct.json(), "gateway": relayed.json(), "mismatch": mismatched.json(), "expired": expired_response.json(), "deleted": rejected.json()})


async def test_isolated_gc_respects_communication_window_and_original_binding(
    lifecycle_case: _Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = lifecycle_case
    from app.core.session_control_store import SessionControlStore
    future = case.clock.now + timedelta(days=3)

    class _OwnerTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr("app.core.session_control_communication_ledger.inbox_writes.datetime", _OwnerTime)
    communication_id = "comm_" + "a" * 32
    with closing(case.service._store(case.target)) as store:
        store.create_or_get_communication_inbox(
            session_id=case.target, communication_id=communication_id,
            source_gateway_id=GATEWAY, source_workspace_id=case.workspace.workspace_id,
            source_session_id=case.source, source_thread_id=case.source_thread,
            target_gateway_id=GATEWAY, target_workspace_id=case.workspace.workspace_id,
            target_thread_id=case.target_thread, kind="question", reply_to_communication_id=None,
            payload_hash="a" * 64, content="原通信", delivery_policy="next_turn", request_id="req_gc_binding",
        )
        store.claim_communication_inbox_admission(communication_id, claim_owner="gc-worker", claim_generation=1)
        store.mark_communication_inbox_execution_bound(communication_id, job_id="job_original", execution_id="execution_original", turn_id="turn_original", claim_owner="gc-worker", claim_generation=1)
        store.create_or_get_lease(operation_kind="communication_target", operation_identity="communication-target|" + communication_id, preimage_hash="a" * 64, recovery_ref=communication_id)
    await case.delete_target()
    isolated = case.workspace.resolver.resolve_operation_replay_node(case.target, now=case.clock.now)
    with closing(SessionControlStore(isolated / "session-control.sqlite", read_only=True)) as store:
        binding = store.get_communication_inbox(communication_id)
        assert (binding.state, binding.job_id, binding.execution_id, binding.turn_id, binding.terminal_outcome) == ("terminal", "job_original", "execution_original", "turn_original", "target-deleted")
        assert store.list_non_terminal_leases() == ()
        deadline = store.communication_recovery_deadline()
        assert deadline == future + timedelta(days=1)
    case.clock.now += timedelta(days=2)
    assert await case.service.observations.collect_expired() == deadline
    assert isolated.is_dir()
    case.clock.now += timedelta(days=3)
    await case.service.observations.collect_expired()
    assert not isolated.exists()


async def test_isolated_gc_refuses_unsettled_other_owner(lifecycle_case: _Case) -> None:
    case = lifecycle_case
    with closing(case.service._store(case.target)) as store:
        lease = store.create_or_get_lease(operation_kind="execution", operation_identity="other-owner-still-active", preimage_hash="a" * 64)
    await case.delete_target()
    isolated = case.workspace.resolver.resolve_operation_replay_node(case.target, now=case.clock.now)
    case.clock.now += timedelta(days=2)
    with pytest.raises(RuntimeError, match="非终态 operation lease"):
        await case.service.observations.collect_expired()
    assert isolated.is_dir()
    from app.core.session_control_store import SessionControlStore
    with closing(SessionControlStore(isolated / "session-control.sqlite", read_only=True)) as store:
        assert store.get_lease(lease.lease_id).state == "active"


@pytest.mark.parametrize("separate_gc", [False, True])
async def test_real_gc_waits_for_live_expired_owner_without_busy_loop(
    lifecycle_case: _Case, monkeypatch: pytest.MonkeyPatch, separate_gc: bool,
) -> None:
    case = lifecycle_case
    original_owner = case.service.observations
    case.projection.executions = [{"job_id": "job_live", "execution_id": "execution_live", "turn_id": "turn_live", "state": "running", "revision": "running"}]
    task = asyncio.create_task(case.service.wait(session_id=case.source, thread_id=case.source_thread, source_call_id="live_expiry", source_call_created_at=case.clock.now, target_session_id=case.target, selector=WaitSelector("job", "job_live"), until="terminal", timeout_seconds=30))
    case.tasks.append(task)
    try:
        async with asyncio.timeout(3):
            while True:
                if case.transport.requests:
                    request = case.transport.requests[-1]
                    with closing(case.service._store(case.target)) as store:
                        observation = store.get_remote_observation(request.observation_id, side="target")
                        if observation is not None and observation.snapshot is not None:
                            break
                await asyncio.sleep(0)
        if separate_gc:
            # 独立 service 没有旧 owner 的内存通知，只能等 OS holder 锁释放。
            case.restore()
        owner = case.service.observations
        count = 0
        original_collect = owner.collect_expired

        async def collect():
            nonlocal count
            count += 1
            return await original_collect()

        monkeypatch.setattr(owner, "collect_expired", collect)
        await case.service.start()
        leases = {}
        for session_id in (case.source, case.target):
            with closing(case.service._store(session_id)) as store:
                leases[session_id] = store.list_non_terminal_leases()
        case.clock.now += timedelta(days=2)
        baseline = count
        owner._gc_changed.set()
        async with asyncio.timeout(3):
            while count == baseline:
                await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        settled_count = count
        await asyncio.sleep(0.1)
        assert count == settled_count and not task.done()
        for session_id in (case.source, case.target):
            with closing(case.service._store(session_id)) as store:
                assert store.list_non_terminal_leases() == leases[session_id]
                assert store.list_remote_observations()
        with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
            await case.service.observations.observe(request)
        case.projection.executions[0].update(state="completed", revision="completed")
        await original_owner.notify()
        with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
            await asyncio.wait_for(task, 3)
        async with asyncio.timeout(3):
            while True:
                remaining = []
                for session_id in (case.source, case.target):
                    with closing(case.service._store(session_id)) as store:
                        remaining.extend(store.list_remote_observations())
                if not remaining:
                    break
                await asyncio.sleep(0)
        async with asyncio.timeout(3):
            while owner._recoveries:
                await asyncio.sleep(0)
        assert not case.service._failure
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await case.service.stop()


async def test_real_startup_recovers_expired_wait_with_original_deadline(
    lifecycle_case: _Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = lifecycle_case
    case.projection.executions = [{"job_id": "job_cold", "execution_id": "execution_cold", "turn_id": "turn_cold", "state": "running", "revision": "running"}]
    pending = asyncio.create_task(case.service.wait(session_id=case.source, thread_id=case.source_thread, source_call_id="cold_expiry", source_call_created_at=case.clock.now, target_session_id=case.target, selector=WaitSelector("job", "job_cold"), until="terminal", timeout_seconds=30))
    case.tasks.append(pending)
    async with asyncio.timeout(3):
        while True:
            if case.transport.requests:
                request = case.transport.requests[-1]
                with closing(case.service._store(case.target)) as store:
                    observation = store.get_remote_observation(request.observation_id, side="target")
                    if observation is not None and observation.snapshot is not None:
                        frozen = observation.snapshot
                        break
            await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    case.clock.now += timedelta(days=2)
    case.clock.origin_id = "restarted-boot"
    case.restore()
    owner = case.service.observations
    original = owner._observe_target
    proof = []

    async def resume(original_request, *, recovery=False):
        result = await original(original_request, recovery=recovery)
        with closing(case.service._store(case.target)) as store:
            current = store.get_remote_observation(original_request.observation_id, side="target")
            proof.append((original_request.query["deadline"], current.snapshot, result["status"]))
        return result

    monkeypatch.setattr(owner, "_observe_target", resume)
    await case.service.start()
    try:
        async with asyncio.timeout(3):
            while True:
                remaining = []
                for session_id in (case.source, case.target):
                    with closing(case.service._store(session_id)) as store:
                        remaining.extend(store.list_remote_observations())
                if not remaining:
                    break
                await asyncio.sleep(0)
        assert proof == [(request.query["deadline"], frozen, "timed_out")]
        assert len(case.transport.requests) == 1
        assert not case.service._failure
        with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
            await owner.observe(request)
    finally:
        await case.service.stop()


@pytest.mark.parametrize("replayed", [False, True])
async def test_real_source_delete_during_reply_closes_live_and_replayed_callback(
    lifecycle_case: _Case, monkeypatch: pytest.MonkeyPatch, replayed: bool,
) -> None:
    case = lifecycle_case
    first = await case.read("source_delete") if replayed else None
    reply = asyncio.Event()
    release = asyncio.Event()
    original_operation = case.transport.operation

    async def delayed_reply(**kwargs):
        result = await original_operation(**kwargs)
        reply.set()
        await release.wait()
        return result

    monkeypatch.setattr(case.transport, "operation", delayed_reply)
    pending = asyncio.create_task(case.read("source_delete"))
    case.tasks.append(pending)
    try:
        await asyncio.wait_for(reply.wait(), 3)
        async with asyncio.timeout(3):
            await case.workspace.resolver.delete_session_subtree(case.source)
        release.set()
        if replayed:
            assert await asyncio.wait_for(pending, 3) == first
        else:
            with pytest.raises(RemoteObservationError, match="^target-deleted$"):
                await asyncio.wait_for(pending, 3)
        from app.core.session_control_store import SessionControlStore
        isolated = case.workspace.resolver.resolve_operation_replay_node(case.source, now=case.clock.now)
        with closing(SessionControlStore(isolated / "session-control.sqlite", read_only=True)) as store:
            assert store.list_non_terminal_leases() == ()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("stage", ["lease", "page"])
async def test_real_gc_transaction_failure_keeps_original_payload_and_leases_after_reopen(
    lifecycle_case: _Case, stage: str,
) -> None:
    case = lifecycle_case
    await case.read("atomic_gc")
    request = case.transport.requests[-1]
    with closing(case.service._store(case.source)) as store:
        original = store.get_remote_observation(request.observation_id, side="source")
        pages = store.list_remote_pages(request.observation_id, side="source")
        leases = tuple(store.find_lease_by_operation(identity) for identity in ("source-observation|" + request.observation_id, "source-page|" + request.page_identity))
        table = "session_operation_leases" if stage == "lease" else "remote_observation_pages"
        when = "OLD.operation_identity LIKE 'source-page|%'" if stage == "lease" else "OLD.side='source'"
        store.connection.execute(f"CREATE TRIGGER fail_gc_delete BEFORE DELETE ON {table} WHEN {when} BEGIN SELECT RAISE(ABORT, 'injected-gc-delete'); END")
    case.clock.now += timedelta(days=2)
    with pytest.raises(sqlite3.IntegrityError, match="injected-gc-delete"):
        await case.service.observations.collect_expired()
    with closing(case.service._store(case.source)) as reopened:
        assert reopened.get_remote_observation(request.observation_id, side="source") == original
        assert reopened.list_remote_pages(request.observation_id, side="source") == pages
        assert tuple(reopened.get_lease(lease.lease_id) for lease in leases) == leases
        assert not reopened.connection.in_transaction
        reopened.connection.execute("DROP TRIGGER fail_gc_delete")
    await case.service.observations.collect_expired()
    for session_id in (case.source, case.target):
        with closing(case.service._store(session_id)) as reopened:
            assert reopened.list_remote_observations() == ()
            assert reopened.connection.execute("SELECT count(*) FROM remote_observation_pages").fetchone()[0] == 0
            assert reopened.connection.execute("SELECT count(*) FROM session_operation_leases WHERE operation_kind='remote_observation'").fetchone()[0] == 0
    with pytest.raises(RemoteObservationError, match="^operation-retry-expired$"):
        await case.read("atomic_gc")
