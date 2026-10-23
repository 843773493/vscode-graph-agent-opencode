"""真实消息上传与 catalog 删除在附件提交边界使用同一 topology 锁序。"""

from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from contextvars import ContextVar
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
from app.core.path_utils import workspace_context
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    _CrossProcessFileLock,
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    history_client as history_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _production_fork_context,
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
    method_name = "_acquire_pin" if boundary == "pin" else "_publish"
    operation = getattr(store, method_name)
    write_staging = store._write_staging

    async def observed_acquire(lock):
        if lock.path == topology_path and lock._exclusive and asyncio.current_task() is deletion:
            delete_attempt.set()
        result = await acquire(lock)
        if selected.get() and lock.path == session_path and lock._exclusive:
            locks.append((_exclusive_available(topology_path), _exclusive_available(session_path)))
            if upload_first:
                entered.set()
                await release.wait()
        return result

    async def selected_operation(**kwargs):
        assert _exclusive_available(topology_path), "正文阶段不得继续持有 topology 锁"
        if not upload_first:
            entered.set()
            await release.wait()
        token = selected.set(True)
        try:
            result = await operation(**kwargs)
        finally:
            selected.reset(token)
        committed.set()
        await continue_upload.wait()
        return result

    def stage_outside_gates(*args, **kwargs):
        assert _exclusive_available(topology_path)
        assert _exclusive_available(session_path)
        return write_staging(*args, **kwargs)

    async def observe(operation_id, phase, *, details=None):
        if operation_id == delete_key and phase == "deletion.catalog_deleting":
            marked.set()
            await continue_delete.wait()

    monkeypatch.setattr(_CrossProcessFileLock, "__aenter__", observed_acquire)
    monkeypatch.setattr(store, method_name, selected_operation)
    monkeypatch.setattr(store, "_write_staging", stage_outside_gates)
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
            refs = store._catalog.list_owner_refs_for_session(session_id, active_only=True)
            assert len(refs) == int(boundary == "publish" and upload_first)
            release.set()
            continue_upload.set()
            with pytest.raises(SessionDeletionPendingError):
                await asyncio.wait_for(upload, 5)
            assert case.runner.saver.read_canonical_items(session_id, thread_id) == ()
            assert case.model_requests == []
            assert container.job_service._jobs == {}
            if not upload_first:
                assert locks == [(False, False)]
            continue_delete.set()
            await asyncio.wait_for(deletion, 10)
            assert not locator.exists()
            assert store._catalog.list_owner_refs_for_session(session_id, active_only=True) == ()
            record = service.path_resolver.catalog_store.get_subtree_delete_record(delete_key)
            assert record.state == "completed" and record.drained_session_ids == (session_id,)
            isolated = store._sessions_root / ".deleting" / delete_key / session_id
            with closing(SessionControlStore(isolated / "session-control.sqlite", read_only=True)) as control:
                assert control.list_non_terminal_leases() == ()
            if boundary == "pin" and not upload_first:
                assert store._catalog.connection.execute("SELECT COUNT(*) FROM attachment_ingest_records").fetchone()[0] == 0
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
            assert store._catalog.list_owner_refs_for_session(parent_id, active_only=True) == ()
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
            "SELECT state FROM fork_attachment_claims WHERE copy_operation_id=?", (capture.copy_operation_id,),
        )] == ["target_deleted"]
        retry = await fork_client.post(endpoint, json=request.model_dump(mode="json"))
        assert retry.status_code == 404, retry.text
        assert not observed["path"].exists()
        assert case.runner.saver.read_canonical_items(source, source_thread) == source_items
        assert len(case.model_admissions) == 1
