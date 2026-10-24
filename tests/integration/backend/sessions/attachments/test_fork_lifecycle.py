"""由真实执行生成附件历史，验证 fork 发布和失败的目录关联结算。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.container import AppContainer
from app.core.resource_uri import parse_attachment_uri
from app.core.trace_middleware import get_current_gateway_id
from app.schemas.internal_v2.session import SessionCreateRequest, SessionDTO
from app.services.infrastructure.attachment_blob_catalog import fork_store
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_published_fork_keeps_directory_after_source_delete_until_target_release(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    creation_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = production_fork_container
    context = _production_fork_context(container)
    source = fork_execution_source
    parent = await context.sessions.create(SessionCreateRequest(title="独立副本容器"))
    request = await _fork_request(context, source.session_id, mode="full_rollout_copy",
        creation_kind=creation_kind, target_container_uri=parent.uri if creation_kind == "new_child_thread" else None)
    result = await context.forks.fork(request)
    store = container.attachment_blob_store
    resource, = result.resources
    target = parse_attachment_uri(resource.target_uri, local_gateway_id=get_current_gateway_id(), require_file=True)
    source_reference = parse_attachment_uri(resource.resource_uri, require_file=True)
    assert target.gateway_authority is None
    assert (target.scope_id, target.attachment_id, target.tail_segments) == (
        source_reference.scope_id, source_reference.attachment_id, source_reference.tail_segments,
    )
    identity = target.attachment_id
    source_thread = context.sessions.path_resolver.main_thread_id(source.session_id)
    original_items = container.checkpointer._storage.storage.read_items(source.session_id, source_thread)
    target_items = container.checkpointer._storage.storage.read_items(result.session.session_id, result.thread_id)
    assert [item.to_dict() for item in target_items] == [item.to_dict() for item in original_items]
    assert store.read(result.session.session_id, resource.target_uri, owner_thread_id=result.thread_id).data == "冷恢复附件正文".encode()
    key = context.forks._creation_key(request.idempotency_key)
    await context.sessions.path_resolver.delete_subtree(
        idempotency_key="delete-shared-source", root_node_id=source.session_id,
    )
    active = [tuple(row) for row in store._catalog.connection.execute(
        "SELECT session_id,thread_id FROM attachment_directory_associations WHERE attachment_id=? AND state='active'",
        (identity,),
    )]
    assert active == [(result.session.session_id, result.thread_id)]
    assert store._catalog.get_directory(identity).gc_after is None
    future = datetime.now(UTC) + timedelta(days=31)
    monkeypatch.setattr(store, "_clock", lambda: future)
    assert await store.collect_garbage(leases=container.external_resource_leases) == ()
    assert store.read(result.session.session_id, resource.resource_uri, owner_thread_id=result.thread_id).data == "冷恢复附件正文".encode()
    assert [item.to_dict() for item in container.checkpointer._storage.storage.read_items(
        result.session.session_id, result.thread_id,
    )] == [item.to_dict() for item in original_items]
    await context.sessions.path_resolver.delete_subtree(
        idempotency_key="delete-last-target", root_node_id=result.session.session_id,
    )
    assert [row[0] for row in store._catalog.connection.execute(
        "SELECT state FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (key,),
    )] == ["target_deleted"]
    record = store._catalog.get_directory(identity)
    assert datetime.fromisoformat(record.unattached_since) == future
    assert datetime.fromisoformat(record.gc_after) == future + timedelta(days=30)
    directory = resolve_attachment_directory(store._attachments_root, record.relative_locator)
    assert directory.is_dir()
    assert await store.collect_garbage(leases=container.external_resource_leases) == ()
    monkeypatch.setattr(store, "_clock", lambda: future + timedelta(days=31))
    assert await store.collect_garbage(leases=container.external_resource_leases) == (identity,)
    assert not directory.exists()
    assert store._catalog.get_directory(identity).availability == "tombstoned"


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_unpublished_fork_failure_releases_only_its_original_directory_claim(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    creation_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = production_fork_container
    context = _production_fork_context(container)
    parent = await context.sessions.create(SessionCreateRequest(title="失败副本容器"))
    request = await _fork_request(context, fork_execution_source.session_id, mode="full_rollout_copy",
        creation_kind=creation_kind, target_container_uri=parent.uri if creation_kind == "new_child_thread" else None)
    key = context.forks._creation_key(request.idempotency_key)
    store = container.attachment_blob_store
    original_associations = [tuple(row) for row in store._catalog.connection.execute("SELECT * FROM attachment_directory_associations")]
    observer = fork_store.observe_phase
    phases: list[str] = []

    async def fail_after_reservation(operation_id: str, phase: str, **kwargs: object) -> None:
        await observer(operation_id, phase, **kwargs)
        if operation_id == key and phase == "copy.attachments.owner_reserved":
            phases.append(phase)
            raise RuntimeError("injected-after-directory-reservation")

    with monkeypatch.context() as failure:
        failure.setattr(fork_store, "observe_phase", fail_after_reservation)
        with pytest.raises(RuntimeError, match="injected-after-directory-reservation"):
            await context.forks.fork(request)
    assert phases == ["copy.attachments.owner_reserved"]
    resolver = context.sessions.path_resolver
    capture = resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
    assert capture.state == "aborted"
    store = container.attachment_blob_store
    claim, = store._catalog.connection.execute(
        "SELECT state,source_attachment_id FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (key,),
    ).fetchall()
    assert claim[0] == "aborted"
    assert [tuple(row) for row in store._catalog.connection.execute(
        "SELECT * FROM attachment_directory_associations",
    )] == original_associations
    if creation_kind == "new_session_main":
        assert resolver.catalog_store.get_creation_record(key).state == "aborted"
        with pytest.raises(KeyError):
            resolver.catalog_store.get_node(capture.target_session_id)
    else:
        threads = context.forks._threads.for_owner_session(parent.session_id)
        record = threads.control_store.get_thread_creation_record(key)
        assert record.state == "aborted"
        session_root = resolver.resolve_session_node(parent.session_id)
        assert not (session_root / record.final_relative_locator).exists()
        assert not (session_root / record.staging_locator).exists()
    # 不修写业务记录；原删除 owner 释放最后关联后，原 GC 不得被失败 claim 无限阻塞。
    now = datetime.now(UTC)
    monkeypatch.setattr(store, "_clock", lambda: now)
    await resolver.delete_subtree(idempotency_key="delete-failed-fork-source", root_node_id=fork_execution_source.session_id)
    monkeypatch.setattr(store, "_clock", lambda: now + timedelta(days=31))
    assert await store.collect_garbage(leases=container.external_resource_leases) == (claim[1],)
    assert store._catalog.get_directory(claim[1]).availability == "tombstoned"
