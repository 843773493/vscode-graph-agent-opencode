"""真实消息上传与 catalog 删除在附件提交边界使用同一 topology 锁序。"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import portalocker
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.api.deps import (
    get_request_id,
    get_session_context_fork_service,
    verify_local_token,
)
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    _CrossProcessFileLock,
)
from app.services.infrastructure.attachment_blob_catalog import store as store_module
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
    resolve_staging_path,
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
    _completed,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _session,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    gateway_skill_publication as gateway_skill_publication,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_compaction_gateway_context as runner_compaction_gateway_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_gateway_context as runner_gateway_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import control_request


def _exclusive_available(path: Path) -> bool:
    with path.open("a+b") as stream:
        try:
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked:
            return False
        portalocker.unlock(stream)
        return True


@pytest.mark.parametrize("boundary", ["pin", "publish"])
@pytest.mark.parametrize("upload_first", [True, False], ids=["upload-first", "delete-first"])
async def test_message_attachment_commit_serializes_with_catalog_delete(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, boundary: str, upload_first: bool,
) -> None:
    case = runtime_case
    container = case.runner.container
    service = container.session_service
    store = container.attachment_blob_store
    session_id, thread_id = await _session(case.runner)
    locator = service.path_resolver.resolve_session_node(session_id)
    control_path = locator / "session-control.sqlite"
    topology_path = service._navigation_topology_gate.exclusive().path
    session_path = service._session_lifecycle_gate.exclusive(session_id).path
    delete_key = f"attachment-{boundary}-{upload_first}"
    entered, release, committed, continue_upload, marked, continue_delete, delete_attempt = (
        asyncio.Event() for _ in range(7)
    )
    selected: ContextVar[bool] = ContextVar("attachment_commit_under_test", default=False)
    locks: list[tuple[bool, bool]] = []
    upload = deletion = None
    acquire = _CrossProcessFileLock.__aenter__
    persist = store._persist_one
    write_directory = store._write_directory
    phase_observer = store_module.observe_phase
    stage: ContextVar[str] = ContextVar("attachment_stage_under_test", default="pin")

    async def observed_acquire(lock):
        if lock.path == topology_path and lock._exclusive and asyncio.current_task() is deletion:
            delete_attempt.set()
        result = await acquire(lock)
        if selected.get() and stage.get() == boundary and lock.path == session_path and lock._exclusive:
            locks.append((_exclusive_available(topology_path), _exclusive_available(session_path)))
            if upload_first:
                entered.set()
                await release.wait()
        return result

    async def selected_operation(*args, **kwargs):
        if boundary == "pin" and not upload_first:
            entered.set()
            await release.wait()
        token = selected.set(True)
        try:
            return await persist(*args, **kwargs)
        finally:
            selected.reset(token)

    async def observed_phase(operation_id, phase, *, details=None):
        if phase == "attachment.ingest.directory_durable":
            stage.set("publish")
            if boundary == "publish" and not upload_first:
                entered.set()
                await release.wait()
        await phase_observer(operation_id, phase, details=details)
        if phase == ("attachment.ingest.pin_acquired" if boundary == "pin" else "attachment.ingest.published"):
            committed.set()
            await continue_upload.wait()

    def stage_outside_gates(*args, **kwargs):
        assert _exclusive_available(topology_path)
        assert _exclusive_available(session_path)
        return write_directory(*args, **kwargs)

    async def observe(operation_id, phase, *, details=None):
        if operation_id == delete_key and phase == "deletion.catalog_deleting":
            marked.set()
            await continue_delete.wait()

    monkeypatch.setattr(_CrossProcessFileLock, "__aenter__", observed_acquire)
    monkeypatch.setattr(store, "_persist_one", selected_operation)
    monkeypatch.setattr(store, "_write_directory", stage_outside_gates)
    monkeypatch.setattr("app.services.infrastructure.attachment_blob_catalog.store.observe_phase", observed_phase)
    monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        with closing(SessionControlStore(control_path, read_only=True)) as control:
            fence = control.get_fence()
            initial_leases = tuple(control.connection.iterdump())
        assert fence[0] == "active"
        upload = asyncio.create_task(history_client.post(
            f"/api/v1/sessions/{session_id}/threads/{thread_id}/messages",
            json={"run": {}, "message": {"content": "删除竞争中的真实上传", "attachments": [{
                "file_id": "inline:race", "name": "race.txt", "content_type": "text/plain",
                "data_url": "data:text/plain;base64," + base64.b64encode(b"attachment-topology").decode(),
            }]}},
        ))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if upload_first:
                assert locks == [(False, False)], "Session 临界区未同时阻止 topology exclusive"
            deletion = asyncio.create_task(service.path_resolver.delete_subtree(
                idempotency_key=delete_key, root_node_id=session_id,
            ))
            if upload_first:
                await asyncio.wait_for(delete_attempt.wait(), 3)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(deletion), 0.05)
                assert not marked.is_set()
                assert service.path_resolver.catalog_store.get_node(session_id).state == "active"
                release.set()
                await asyncio.wait_for(committed.wait(), 3)
            await asyncio.wait_for(marked.wait(), 5)
            assert service.path_resolver.catalog_store.get_node(session_id).state == "deleting"
            with closing(SessionControlStore(control_path, read_only=True)) as control:
                assert control.get_fence() == fence, "必须覆盖 catalog 已关闭而 local fence 仍 active 的窗口"
                pins = control.list_non_terminal_leases()
                if boundary == "pin" and not upload_first:
                    assert tuple(control.connection.iterdump()) == initial_leases
                else:
                    assert len(pins) == 1 and pins[0].operation_kind == "attachment"
            refs = store._catalog.connection.execute("SELECT * FROM attachment_directory_associations WHERE session_id=? AND state='active'", (session_id,)).fetchall()
            assert len(refs) == int(boundary == "publish" and upload_first)
            release.set()
            continue_upload.set()
            rejected = await asyncio.wait_for(upload, 5)
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["detail"]
            assert case.runner.saver.read_canonical_items(session_id, thread_id) == ()
            assert case.model_requests == []
            assert container.job_service._jobs == {}
            if not upload_first:
                assert locks == [(False, False)]
            continue_delete.set()
            await asyncio.wait_for(deletion, 10)
            assert not locator.exists()
            assert store._catalog.connection.execute("SELECT * FROM attachment_directory_associations WHERE session_id=? AND state='active'", (session_id,)).fetchall() == []
            record = service.path_resolver.catalog_store.get_subtree_delete_record(delete_key)
            assert record.state == "completed" and record.drained_session_ids == (session_id,)
            isolated = store._sessions_root / ".deleting" / delete_key / session_id
            with closing(SessionControlStore(isolated / "session-control.sqlite", read_only=True)) as control:
                assert control.list_non_terminal_leases() == ()
            if boundary == "pin" and not upload_first:
                assert store._catalog.connection.execute("SELECT COUNT(*) FROM attachment_directory_ingests").fetchone()[0] == 0
        finally:
            release.set()
            continue_upload.set()
            continue_delete.set()
            await asyncio.gather(*(task for task in (upload, deletion) if task is not None), return_exceptions=True)


@pytest.fixture
async def fork_client(runtime_case: RuntimeCase, runner_compaction_gateway_context, gateway_skill_publication):
    application = FastAPI()
    application.include_router(sessions.router, prefix="/api/v1")
    container = runtime_case.runner.container
    application.dependency_overrides[get_session_context_fork_service] = lambda: container.session_context_fork_service
    application.dependency_overrides[get_request_id] = lambda: "attachment-topology-fork"
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://workspace") as client:
        yield client


async def _source_with_attachment(case: RuntimeCase, client: httpx.AsyncClient) -> tuple[str, str]:
    session_id, thread_id = await _session(case.runner)
    response = await client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "run": {}, "message": {"content": "生成可 fork 的真实附件历史", "attachments": [{
            "file_id": "inline:source", "name": "source.txt", "content_type": "text/plain",
            "data_url": "data:text/plain;base64," + base64.b64encode(b"fork-topology-source").decode(),
        }]},
    })
    assert response.status_code == 200, response.text
    job = case.runner.container.job_service._jobs[response.json()["data"]["job_id"]]
    await asyncio.wait_for(asyncio.shield(job.task), 20)
    assert job.status.value == "completed", job.error_message
    return session_id, thread_id


@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("boundary", ["reserve", "finalize"])
async def test_real_fork_attachment_transaction_holds_topology_until_commit(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, fork_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, creation_kind: str, boundary: str,
) -> None:
    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        source_id, source_thread = await _source_with_attachment(case, history_client)
        original_items = case.runner.saver.read_canonical_items(source_id, source_thread)
        request = await _fork_request(
            _production_fork_context(container), source_id,
            mode="full_rollout_copy", creation_kind=creation_kind,
        )
        operation_name = "reserve_copy_attachments" if boundary == "reserve" else "finalize_copy_attachments"
        operation = getattr(store, operation_name)
        selected: ContextVar[str | None] = ContextVar("fork_attachment_target", default=None)
        topology = container.session_service._navigation_topology_gate.exclusive().path
        acquire = _CrossProcessFileLock.__aenter__
        locks = []

        async def observed_acquire(lock):
            result = await acquire(lock)
            target = selected.get()
            if target is not None and lock.path == store._gate.exclusive(target).path and lock._exclusive:
                locks.append((_exclusive_available(topology), _exclusive_available(lock.path)))
            return result

        async def selected_operation(capture, *args, **kwargs):
            assert _exclusive_available(topology)
            token = selected.set(capture.target_session_id)
            try:
                return await operation(capture, *args, **kwargs)
            finally:
                selected.reset(token)
                assert _exclusive_available(topology)

        monkeypatch.setattr(_CrossProcessFileLock, "__aenter__", observed_acquire)
        monkeypatch.setattr(store, operation_name, selected_operation)
        response = await asyncio.wait_for(fork_client.post(
            f"/api/v1/sessions/{source_id}/fork-context", json=request.model_dump(mode="json"),
        ), 25)
        assert response.status_code == 200, response.text
        assert locks == [(False, False)]
        result = response.json()["data"]
        assert result["creation_kind"] == creation_kind
        assert len(result["resources"]) == 1
        target_id, target_thread = result["session"]["session_id"], result["thread_id"]
        assert store.read(target_id, result["resources"][0]["target_uri"], owner_thread_id=target_thread).data == b"fork-topology-source"
        assert case.runner.saver.read_canonical_items(source_id, source_thread) == original_items


async def test_child_fork_reservation_rejects_catalog_delete_with_active_local_fence(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, fork_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    service = container.session_service
    marked, proceed = asyncio.Event(), asyncio.Event()
    deletion = None
    observed = []
    reserve = store.reserve_copy_attachments
    delete_key = "child-fork-reservation-delete"

    async def observe(operation_id, phase, *, details=None):
        if operation_id == delete_key and phase == "deletion.catalog_deleting":
            marked.set()
            await proceed.wait()

    async def delete_before_reservation(capture, creation, snapshots):
        nonlocal deletion
        locator = service.path_resolver.resolve_session_node(capture.target_session_id)
        deletion = asyncio.create_task(service.path_resolver.delete_subtree(
            idempotency_key=delete_key, root_node_id=capture.target_session_id,
        ))
        await asyncio.wait_for(marked.wait(), 5)
        assert service.path_resolver.catalog_store.get_node(capture.target_session_id).state == "deleting"
        with closing(SessionControlStore(locator / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence()[0] == "active"
            before_control = tuple(control.connection.iterdump())
        before_attachments = tuple(store._catalog.connection.iterdump())
        with pytest.raises(SessionDeletionPendingError, match="catalog 已关闭"):
            await reserve(capture, creation, snapshots)
        assert tuple(store._catalog.connection.iterdump()) == before_attachments
        with closing(SessionControlStore(locator / "session-control.sqlite", read_only=True)) as control:
            assert tuple(control.connection.iterdump()) == before_control
        observed.append(capture.copy_operation_id)
        raise SessionDeletionPendingError("child reservation 因 catalog 删除被拒绝")

    monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)
    monkeypatch.setattr(store, "reserve_copy_attachments", delete_before_reservation)
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        source_id, source_thread = await _source_with_attachment(case, history_client)
        parent_id, _ = await _session(case.runner)
        parent = await service.get(parent_id)
        original_items = case.runner.saver.read_canonical_items(source_id, source_thread)
        request = await _fork_request(
            _production_fork_context(container), source_id, mode="full_rollout_copy",
            creation_kind="new_child_thread", target_container_uri=parent.uri,
        )
        try:
            response = await asyncio.wait_for(fork_client.post(
                f"/api/v1/sessions/{source_id}/fork-context", json=request.model_dump(mode="json"),
            ), 25)
            assert response.status_code == 409, response.text
            assert len(observed) == 1
            assert store._catalog.connection.execute("SELECT * FROM attachment_directory_associations WHERE session_id=? AND state='active'", (parent_id,)).fetchall() == []
            proceed.set()
            await asyncio.wait_for(deletion, 10)
            assert case.runner.saver.read_canonical_items(source_id, source_thread) == original_items
        finally:
            proceed.set()
            if deletion is not None:
                await asyncio.gather(deletion, return_exceptions=True)


@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("replay", [False, True], ids=["fresh", "published-replay"])
@pytest.mark.parametrize("commit_first", [True, False], ids=["commit-first", "delete-first"])
async def test_product_fork_reply_follows_attachment_commit_before_target_delete(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, fork_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, creation_kind: str, replay: bool, commit_first: bool,
) -> None:
    """真实消息与图生成来源；提交后的回执保留，删除先行则拒绝新的附件发布。"""
    case = runtime_case
    container = case.runner.container
    store = container.attachment_blob_store
    resolver = container.session_service.path_resolver
    catalog = resolver.catalog_store
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        source, source_thread = await _source_with_attachment(case, history_client)
        source_items = case.runner.saver.read_canonical_items(source, source_thread)
        target_uri = None
        if creation_kind == "new_child_thread":
            parent, _ = await _session(case.runner)
            target_uri = (await container.session_service.get(parent)).uri
        request = await _fork_request(
            _production_fork_context(container), source, mode="full_rollout_copy",
            creation_kind=creation_kind, target_container_uri=target_uri,
        )
        endpoint = f"/api/v1/sessions/{source}/fork-context"
        if replay:
            first = await fork_client.post(endpoint, json=request.model_dump(mode="json"))
            assert first.status_code == 200, first.text
            result = first.json()["data"]
            assert store.read(result["session"]["session_id"], result["resources"][0]["target_uri"],
                              owner_thread_id=result["thread_id"]).data == b"fork-topology-source"
        finalizer = store.finalize_copy_attachments
        entered, release = asyncio.Event(), asyncio.Event()
        observed = {}
        pending = None

        async def observe(operation_id, phase, *, details=None):
            if phase == "deletion.catalog_deleting":
                entered.set()
                await release.wait()

        async def ordered_finalizer(capture, *, publication_preimage_hash):
            nonlocal pending
            observed["capture"] = capture
            observed["path"] = resolver.resolve_session_node(capture.target_session_id)
            observed["resources"] = store.copy_resource_results(capture)
            key = "fork-reply-delete-" + capture.target_session_id
            if commit_first:
                await finalizer(capture, publication_preimage_hash=publication_preimage_hash)
                await resolver.delete_subtree(idempotency_key=key, root_node_id=capture.target_session_id)
            else:
                pending = asyncio.create_task(resolver.delete_subtree(
                    idempotency_key=key, root_node_id=capture.target_session_id,
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    assert catalog.get_node(capture.target_session_id).state == "deleting"
                    with closing(store._open_control_store(capture.target_session_id)) as control:
                        assert control.get_fence()[0] == "active"
                    await finalizer(capture, publication_preimage_hash=publication_preimage_hash)
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.shield(pending), 10)
            assert not observed["path"].exists()

        monkeypatch.setattr(store, "finalize_copy_attachments", ordered_finalizer)
        if not commit_first:
            monkeypatch.setattr("app.core.session_subtree_delete.observe_phase", observe)
        try:
            response = await asyncio.wait_for(fork_client.post(endpoint, json=request.model_dump(mode="json")), 25)
        finally:
            release.set()
            if pending is not None:
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 10)
        assert response.status_code == (200 if commit_first else 409), response.text
        capture = observed["capture"]
        if commit_first:
            result = response.json()["data"]
            assert (result["session"]["session_id"], result["thread_id"], result["creation_kind"]) == (
                capture.target_session_id, capture.target_thread_id, creation_kind,
            )
            assert result["resources"] == observed["resources"]
        else:
            assert "Session catalog 节点非 active" in response.json()["detail"]
        assert not observed["path"].exists()
        assert catalog.get_fork_source_capture(copy_operation_id=capture.copy_operation_id).state == "materialized"
        assert [row[0] for row in store._catalog.connection.execute(
            "SELECT state FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (capture.copy_operation_id,),
        )] == ["target_deleted"]
        retry = await fork_client.post(endpoint, json=request.model_dump(mode="json"))
        assert retry.status_code == 404, retry.text
        assert not observed["path"].exists()
        assert case.runner.saver.read_canonical_items(source, source_thread) == source_items
        assert len(case.model_admissions) == 1


def _upload_state(case: CommunicationCase, session: str) -> dict[str, list[dict[str, object]]]:
    """仅核对真实 owner 已提交的事实，不替上传或恢复生成状态。"""
    node = case.main_thread_node(session)
    session_node = get_session_path_resolver(case.workspace / ".boxteam/sessions").resolve_session_node(session)
    queries = {
        session_node / "session-control.sqlite": {
            "pins": "SELECT * FROM session_operation_leases WHERE operation_kind='attachment' ORDER BY lease_id",
        },
        case.workspace / ".boxteam/attachments/catalog.sqlite": {
            "ingests": "SELECT * FROM attachment_directory_ingests ORDER BY ingest_id",
            "refs": "SELECT * FROM attachment_directory_associations ORDER BY attachment_id,session_id,thread_id",
            "directories": "SELECT * FROM attachment_directories ORDER BY attachment_id",
            "files": "SELECT * FROM attachment_directory_files ORDER BY attachment_id,relative_path",
        },
        node / "rollout/index.sqlite": {
            "acceptances": "SELECT * FROM turn_acceptances ORDER BY created_at,turn_id",
            "roots": "SELECT item_id,item_sequence,turn_id,content_hash FROM item_catalog "
                     "WHERE semantic_kind='user_input' ORDER BY item_sequence",
            "queue_identity": "SELECT job_id,execution_id,admission_commit_id,input_hash,input_json,created_at "
                              "FROM execution_queue ORDER BY admission_commit_id",
            "queue": "SELECT job_id,phase FROM execution_queue ORDER BY admission_commit_id",
        },
    }
    state = {}
    for database, tables in queries.items():
        if database in {node / "rollout/index.sqlite", case.workspace / ".boxteam/attachments/catalog.sqlite"}:
            # 上传接受前允许尚未物化 rollout；显式记录缺库，不能靠读探针创建业务库。
            state["rollout_database" if database == node / "rollout/index.sqlite" else "attachment_database"] = [{"exists": database.exists()}]
            if not database.exists():
                state.update({name: [] for name in tables})
                continue
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            state.update({name: [dict(row) for row in connection.execute(query)] for name, query in tables.items()})
    return state


@pytest.mark.parametrize("boundary", [
    "pin_acquired", "preparing", "directory_staged", "directory_renamed", "directory_durable", "published", "accepted",
])
async def test_gateway_attachment_upload_recovers_after_process_loss(
    communication_case: CommunicationCase, boundary: str,
) -> None:
    """从 Gateway 上传到真实 Agent，硬退出后只恢复原接受事实。"""
    case = communication_case
    control = case.replay.control
    session = await create_session(case.client, "附件上传进程恢复")
    call, = await case.plan(session, [{"content": "attachment-recovered-result"}])
    thread = case.planned_calls[call]["thread_id"]
    session_node = await case.session_node(session)
    barrier = "upload-durable-boundary"
    control.arm_match(barrier, "attachment.ingest." + boundary, {"session_id": session, "thread_id": thread})
    body = b"gateway-attachment-durable-recovery"
    payload = {"run": {}, "message": {"content": "读取恢复后的附件", "attachments": [{
        "file_id": "inline:process-loss", "name": "recovery.txt", "content_type": "text/plain",
        "data_url": "data:text/plain;base64," + base64.b64encode(body).decode(),
    }]}}
    upload = asyncio.create_task(case.client.post(f"/api/v1/sessions/{session}/messages", json=payload))
    try:
        crash = await asyncio.to_thread(control.acknowledged_match, barrier)
        assert crash["pid"] == case.backend.process.pid
        assert set(crash["details"]) == {"session_id", "thread_id", "message_id", "lease_id"} | ({"attachment_id"} if boundary != "pin_acquired" else set())
        before = _upload_state(case, session)
        pin, = before["pins"]
        assert pin["state"] == "active"
        assert pin["lease_id"] == crash["details"]["lease_id"]
        assert pin["operation_identity"] == crash["operation_id"]
        assert pin["recovery_ref"] == f"attachment:{session}:{thread}:{crash['details']['message_id']}"
        accepted = boundary == "accepted"
        published = boundary in {"published", "accepted"}
        assert before["rollout_database"] == [{"exists": accepted}]
        assert len(before["acceptances"]) == len(before["roots"]) == len(before["queue_identity"]) == int(accepted)
        assert len(before["refs"]) == len(before["directories"]) == int(published)
        assert before["queue"] == ([{"job_id": before["queue_identity"][0]["job_id"], "phase": "queued"}] if accepted else [])
        attachments_root = case.workspace / ".boxteam/attachments"
        staging = None
        if boundary == "pin_acquired":
            assert before["ingests"] == before["files"] == []
        else:
            ingest, = before["ingests"]
            assert ingest["pin_lease_id"] == pin["lease_id"]
            assert ingest["owner_session_id"] == session and ingest["owner_thread_id"] == thread
            assert ingest["state"] == ("published" if published else "durable" if boundary == "directory_durable" else "preparing")
            staging = resolve_staging_path(attachments_root, ingest["staging_relative_locator"])
            assert staging.exists() == (boundary == "directory_staged")
            directory = resolve_attachment_directory(attachments_root, ingest["relative_locator"])
            assert directory.exists() == (boundary in {"directory_renamed", "directory_durable", "published", "accepted"})
            if directory.exists():
                assert (directory / "original/content").read_bytes() == body
        assert not (case.replay.artifacts / (call + ".json")).exists()
        case.record("upload-before-crash", {"boundary": boundary, "state": before, "ack": crash})

        # accepted 屏障位于同步准入调用内；由 pytest 硬杀，不向被屏障阻塞的 Backend 发 HTTP。
        await case.hard_restart()
        response, = await asyncio.gather(upload, return_exceptions=True)
        assert isinstance(response, httpx.TransportError) or isinstance(response, httpx.Response) and response.status_code >= 500
        after = _upload_state(case, session)
        recovered, = after["pins"]
        for field in ("lease_id", "operation_identity", "preimage_hash", "captured_lifecycle_generation", "recovery_ref", "created_at"):
            assert recovered[field] == pin[field], field
        assert recovered["fencing_token"] == pin["fencing_token"] + 1
        assert recovered["state"] == ("completed" if accepted else "failed")
        assert not staging or not staging.exists()
        assert len(after["ingests"]) == len(before["ingests"])
        if after["ingests"]:
            assert after["ingests"][0]["ingest_id"] == before["ingests"][0]["ingest_id"]
            assert after["ingests"][0]["state"] == ("published" if published else "aborted")
        assert [ref["state"] for ref in after["refs"]] == (["active" if accepted else "released"] if published else [])
        for name in ("acceptances", "roots", "queue_identity"):
            assert after[name] == before[name], name
        case.record("upload-recovered", {"boundary": boundary, "state": after})
        if accepted:
            job_id = before["queue_identity"][0]["job_id"]
        else:
            assert not (case.replay.artifacts / (call + ".json")).exists()
            # 未接受的上传明确失败，重新走相同产品入口上传，不能替产品补写准入。
            retry = await case.client.post(f"/api/v1/sessions/{session}/messages", json=payload)
            assert retry.status_code == 200, retry.text
            job_id = retry.json()["data"]["job_id"]
        await _completed(case.client, job_id)
        history = await case.client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 1})
        assert history.status_code == 200, history.text
        turn, = history.json()["data"]["items"]
        message, = turn["user_messages"]
        attachment, = message["attachments"]
        assert history.json()["data"]["thread_id"] == thread and turn["job_id"] == job_id
        assert turn["final_response"] == "attachment-recovered-result"
        assert message["content"] == payload["message"]["content"]
        assert (message["message_id"] == crash["details"]["message_id"]) == accepted
        assert "data_url" not in attachment
        file_id = attachment["file_id"]
        content = await case.client.get(f"/api/v1/sessions/{session}/attachments/content", params={"file_id": file_id})
        assert content.status_code == 200 and content.content == body
        assert file_id in json.dumps(case.request(call)["messages"], ensure_ascii=False)
        final = _upload_state(case, session)
        assert len(final["acceptances"]) == len(final["roots"]) == len(final["queue"]) == 1
        assert final["queue"] == [{"job_id": job_id, "phase": "terminal"}]
        # 已发布但未接受的上传进入30天宽限期；重新上传拥有另一个独立目录。
        assert len(final["directories"]) == len(final["files"]) == 1 + int(boundary == "published")
        assert len([ref for ref in final["refs"] if ref["state"] == "active"]) == 1
        assert all(row["state"] in {"failed", "completed"} for row in final["pins"])
        assert len(final["pins"]) == (1 if accepted else 2)

        await case.hard_restart()
        settled = _upload_state(case, session)
        for name in final.keys() - {"refs", "directories"}:
            assert settled[name] == final[name], name
        released_at = {}
        assert len(settled["refs"]) == len(final["refs"])
        for previous, current in zip(final["refs"], settled["refs"], strict=True):
            if previous["state"] == "active":
                # 执行已终结，重启对账必须释放旧 runtime，不能永久保活原件。
                assert previous["binding_token"].startswith("runtime:")
                assert datetime.fromisoformat(current["updated_at"]) >= datetime.fromisoformat(previous["updated_at"])
                assert current == {**previous, "state": "released", "updated_at": current["updated_at"]}
                released_at[current["attachment_id"]] = current["updated_at"]
            else:
                assert current == previous
        assert len(settled["directories"]) == len(final["directories"])
        for previous, current in zip(final["directories"], settled["directories"], strict=True):
            timestamp = released_at.get(previous["attachment_id"])
            if timestamp is None:
                assert current == previous
            else:
                assert previous["unattached_since"] is None and previous["gc_after"] is None
                assert current == {**previous, "unattached_since": timestamp, "updated_at": timestamp,
                                   "gc_after": (datetime.fromisoformat(timestamp) + timedelta(days=30)).isoformat()}
        if accepted:
            await case.hard_restart()
            assert _upload_state(case, session) == settled
        content = await case.client.get(f"/api/v1/sessions/{session}/attachments/content", params={"file_id": file_id})
        assert content.status_code == 200 and content.content == body
        assert [event["operation_id"] for event in control.events if event["phase"] == "model.response_ready"] == [call]
        assert session_node.exists()
        case.record("upload-terminal-after-restarts", {"state": settled, "history": history.json()})
    finally:
        control_request(json.loads(control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": barrier,
        })
        if not upload.done():
            upload.cancel()
        await asyncio.gather(upload, return_exceptions=True)
