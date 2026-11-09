"""产品发布后的资源锁等待到期保留目标，原key重试只关联一次。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import main
from app.abstractions.workspace_resources import ResourceKind
from app.container import AppContainer
from app.schemas.internal_v2.session import SessionDTO
from app.services.business import session_context_fork_service as fork_module
from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.records import ThreadRef
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _create_fork_live_resource,
    _fork_live_choice,
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    live_resource_transport_factory as live_resource_transport_factory,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_published_fork_lock_timeout_and_same_key_retry(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    live_resource_transport_factory, monkeypatch: pytest.MonkeyPatch, inprocess_model_replay,
    kind: ResourceKind,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    external = live_resource_transport_factory(production_fork_container, kind)
    owner, resource = await _create_fork_live_resource(production_fork_container, kind, source.session_id)
    rid = str(resource["resource_id"])
    request = (await _fork_request(context, source.session_id,
        creation_kind="new_session_main", mode="full_rollout_copy")).model_copy(
        update={"resources": [_fork_live_choice(resource)]})
    key = context.forks._creation_key(request.idempotency_key)
    catalog = context.sessions.path_resolver.catalog_store
    lock = owner._resource_lock(rid)
    entered = asyncio.Event()
    deadlines: list[asyncio.Timeout] = []
    observe = fork_module.observe_phase
    armed = False
    armed_task: asyncio.Task[object] | None = None
    ledger_path = owner._leases._state_path
    assert ledger_path is not None
    _, controller = inprocess_model_replay
    calls = controller.transport.call_count

    async def hold(operation_id: str, phase: str, *, details: dict[str, object] | None = None) -> None:
        nonlocal armed, armed_task
        if operation_id == key and phase == f"fork.live.before_attach.{kind}":
            await lock.acquire()
            armed = True
            armed_task = asyncio.current_task()
            entered.set()
        await observe(operation_id, phase, details=details)

    def capture(seconds: float) -> asyncio.Timeout:
        context = asyncio.timeout(seconds)
        if armed and asyncio.current_task() is armed_task:
            assert seconds == 30
            deadlines.append(context)
        return context

    monkeypatch.setattr(fork_module, "observe_phase", hold)
    monkeypatch.setattr(owner_module, "timeout", capture)
    async with main.lifespan(main.app) as _main_lifespan_state_85, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": source.workspace_id,
                 "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84",
                 "X-Request-ID": "fork-live-deadline"},
    ) as client:
        await _main_lifespan_state_85["after_http_startup"]()
        post = asyncio.create_task(client.post(f"/api/v1/sessions/{source.session_id}/fork-context",
                                              json=request.model_dump(mode="json")))
        try:
            await asyncio.wait_for(entered.wait(), 20)
            await asyncio.sleep(0)
            before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
            published = catalog.get_creation_record(key)
            assert published.state == "published"
            if deadlines:
                deadline = deadlines[0].when()
                assert deadline is not None
                deadlines[0].reschedule(deadline - 31)
            response = await asyncio.wait_for(asyncio.shield(post), 2)
            assert response.status_code == 200, response.text
            assert len(deadlines) == 1 and deadlines[0].expired()
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == "fork-live-deadline"
            result = response.json()["data"]
            live, = [item for item in result["resources"] if "owner_reference" in item]
            assert live["availability"] == "unavailable" and live["operation_state"] == "not_dispatched"
            assert "error=TimeoutError" in live["reason"] and str(context.workspace) not in response.text
            assert published.session_id == result["session"]["session_id"]
            assert catalog.get_creation_record(key) == published
            assert owner._store.path.read_bytes() == before and ledger_path.read_bytes() == ledger
            target = ThreadRef(result["session"]["session_id"], result["thread_id"])
            assert owner.fork_attach_receipt(rid, creation_key=key, expected_revision=1, thread=target) is None
            assert not owner._leases.leases_for_resource(rid)
            assert lock.locked() and not lock._waiters
            armed = False
            monkeypatch.setattr(fork_module, "observe_phase", observe)
            lock.release()
            await asyncio.sleep(0)
            assert owner._store.path.read_bytes() == before and external.revisions == []
            replay = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
            assert replay.status_code == 200, replay.text
            replayed = replay.json()["data"]
            current, = [item for item in replayed["resources"] if "owner_reference" in item]
            assert replayed["thread_id"] == result["thread_id"]
            assert current["availability"] == "available" and current["operation_state"] == "committed"
            assert current["committed_revision"] == 2 and external.revisions == [(rid, 2)]
            record = owner._store.get(rid)
            assert record is not None and record.thread_associations.count(target) == 1
            repeated = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
            assert repeated.status_code == 200 and repeated.json()["data"]["thread_id"] == result["thread_id"]
            assert catalog.get_creation_record(key) == published and external.revisions == [(rid, 2)]
            assert external.creates == 1 and controller.transport.call_count == calls
        finally:
            armed = False
            monkeypatch.setattr(fork_module, "observe_phase", observe)
            post.cancel()
            await asyncio.gather(post, return_exceptions=True)
            if lock.locked():
                lock.release()
            # 红测退出后也通过原产品入口结算已发布的目标，不手填业务状态。
            await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
