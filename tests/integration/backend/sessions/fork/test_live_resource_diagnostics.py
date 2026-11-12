"""真实 fork 发布后的外设失败保留原租约与关联事实。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import httpx
import pytest

from app import main
from app.abstractions.workspace_resources import ResourceKind, ResourceSnapshot
from app.container import AppContainer
from app.schemas.internal_v2.session import SessionDTO
from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.records import (
    ResourceRecordStore,
    ThreadRef,
)
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


@pytest.mark.skipif(os.name != "posix", reason="循环符号链接的原生 RuntimeError 仅在 POSIX 核验")
@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("fault", ["dispatched", "committed"])
async def test_public_fork_native_failure_preserves_live_resource_facts(
    production_fork_container: AppContainer,
    fork_execution_source: SessionDTO,
    fork_diagnostic_sink: tuple[Path, list[BaseException]] | None,
    live_resource_transport_factory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    inprocess_model_replay,
    kind: ResourceKind,
    fault: Literal["dispatched", "committed"],
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
    loop = context.workspace / "private-live-native-loop"
    loop.symlink_to(loop.name)
    armed = False
    errors: list[RuntimeError] = []
    original_get, original_list = owner._transport.get_resource, owner._transport.list_resources
    original_phase = owner_module.observe_phase
    _, controller = inprocess_model_replay
    calls = controller.transport.call_count

    def native_failure() -> None:
        try:
            loop.resolve(strict=True)
        except RuntimeError as error:
            errors.append(error)
            raise
        raise AssertionError("循环链接必须触发原生解析失败")

    async def get_resource(resource_id: str) -> ResourceSnapshot:
        if armed and fault == "dispatched":
            native_failure()
        return await original_get(resource_id)

    async def list_resources() -> list[ResourceSnapshot]:
        if armed and fault == "committed":
            native_failure()
        return await original_list()

    async def arm(operation_id: str, phase: str, *, details: dict[str, object] | None = None) -> None:
        nonlocal armed
        if operation_id == key and phase == f"fork.live.{fault}.{kind}":
            armed = True
        await original_phase(operation_id, phase, details=details)

    monkeypatch.setattr(owner._transport, "get_resource", get_resource)
    monkeypatch.setattr(owner._transport, "list_resources", list_resources)
    monkeypatch.setattr(owner_module, "observe_phase", arm)
    async with main.lifespan(main.app) as _main_lifespan_state, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": source.workspace_id,
                 "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84",
                 "X-Request-ID": "fork-live-diagnostics"},
    ) as client:
        await _main_lifespan_state["after_http_startup"]()
        try:
            response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == "fork-live-diagnostics"
            result = response.json()["data"]
            live, = [item for item in result["resources"] if "owner_reference" in item]
            assert live["availability"] == "unavailable"
            assert "private-live-native-loop" not in response.text
            assert len(errors) == 1 and str(loop) in str(errors[0])
            catalog = context.sessions.path_resolver.catalog_store
            published = catalog.get_creation_record(key)
            assert published.state == "published" and published.session_id == result["session"]["session_id"]
            record = ResourceRecordStore(workspace_root=context.workspace, workspace_id=source.workspace_id, kind=kind).get(rid)
            assert record is not None
            assert "private-live-native-loop" not in owner._store.path.read_text()
            target = ThreadRef(result["session"]["session_id"], result["thread_id"])
            receipt = owner.fork_attach_receipt(rid, creation_key=key, thread=target, expected_revision=1)
            assert receipt is not None
            if fault == "dispatched":
                assert live["operation_state"] == receipt.state == "dispatching"
                assert "committed_revision" not in live and receipt.committed_revision is None
                assert record.reconciliation_status == "unknown"
                assert target not in record.thread_associations
                lease, = owner._leases.leases_for_resource(rid)
                assert lease.lease_id == receipt.lease_id and lease.status == "reconcile_required"
                assert "error=RuntimeError" in (record.error or "") and "error=RuntimeError" in (receipt.error or "")
            else:
                assert live["operation_state"] == receipt.state == "committed"
                assert live["committed_revision"] == receipt.committed_revision == record.owner_revision == 2
                assert target in record.thread_associations and owner._leases.leases_for_resource(rid) == []
                assert "stage=fork_live_settlement" in live["reason"] and "error=RuntimeError" in live["reason"]
            if fork_diagnostic_sink is None:
                assert any(item.exc_info and item.exc_info[1] is errors[0] for item in caplog.records)
            else:
                blocked, logged = fork_diagnostic_sink
                assert logged == [errors[0]] * (2 if fault == "dispatched" else 1)
                assert str(blocked) in "\n".join(errors[0].__notes__)
                assert "diagnostic_sink_error=NotADirectoryError" in live["reason"]
            armed = False
            monkeypatch.setattr(owner_module, "observe_phase", original_phase)
            revisions = list(external.revisions)
            replay = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
            assert replay.status_code == 200, replay.text
            replayed = replay.json()["data"]
            current, = [item for item in replayed["resources"] if "owner_reference" in item]
            assert replayed["thread_id"] == result["thread_id"]
            assert current["operation_state"] == receipt.state
            assert current["availability"] == ("unavailable" if fault == "dispatched" else "available")
            assert catalog.get_creation_record(key) == published
            assert external.revisions == revisions and external.creates == 1
            assert controller.transport.call_count == calls
            assert "private-live-native-loop" not in json.dumps(replayed)
        finally:
            armed = False
            monkeypatch.setattr(owner_module, "observe_phase", original_phase)
            # 反例失败后也由产品入口结清原fork期限，不重开操作。
            await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
