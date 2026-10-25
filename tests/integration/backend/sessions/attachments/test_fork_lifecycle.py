"""由真实执行生成附件历史，验证 fork 发布和失败的目录关联结算。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock

import pytest

from app.container import AppContainer
from app.core.resource_uri import (
    parse_attachment_uri,
    resource_display_uri,
    thread_resource_uri,
)
from app.core.trace_middleware import (
    get_current_gateway_id,
    reset_current_gateway_id,
    set_current_gateway_id,
)
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionDTO,
    SessionForkResourceChoice,
)
from app.services.infrastructure.attachment_blob_catalog import fork_import, fork_store
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    SourceForkOwnerTransport,
    _fork_request,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    remote_fork_target as remote_fork_target,  # noqa: PLC0414
)


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("gc_first", [True, False], ids=["gc-first", "fork-first"])
async def test_directory_claim_and_gc_choose_one_order_before_target_publication(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    creation_kind: str, gc_first: bool, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    container = production_fork_container
    context = _production_fork_context(container)
    source = fork_execution_source
    store = container.attachment_blob_store
    source_thread = context.sessions.path_resolver.main_thread_id(source.session_id)
    canonical = context.sessions.path_resolver.resolve_thread_node(
        source.session_id, source_thread,
    ) / "rollout/rollout.jsonl"
    original_history = canonical.read_bytes()
    original, = store._catalog.gc_candidates(now=datetime.now(UTC) + timedelta(days=31))
    assert original.gc_after is not None
    directory = resolve_attachment_directory(store._attachments_root, original.relative_locator)
    monkeypatch.setattr(store, "_clock", lambda: datetime.fromisoformat(original.gc_after))
    parent = await context.sessions.create(SessionCreateRequest(title="回收竞争的副本容器"))
    fork_request = await _fork_request(
        context, source.session_id, mode="full_rollout_copy", creation_kind=creation_kind,
        target_container_uri=parent.uri if creation_kind == "new_child_thread" else None,
    )
    key = context.forks._creation_key(fork_request.idempotency_key)
    program = dedent("""\
        import asyncio
        import json
        import sys
        from datetime import datetime
        from pathlib import Path
        from app.core.path_utils import build_session_catalog_components
        from app.core.workspace_identity import read_workspace_id
        from app.services.infrastructure.attachment_blob_catalog.store import AttachmentBlobStore
        from app.services.infrastructure.external_resource_leases import ExternalResourceLeaseLedger
        root = Path(sys.argv[1])
        clock = datetime.fromisoformat(sys.argv[2])
        catalog = build_session_catalog_components(root / ".boxteam/sessions", read_workspace_id(root))
        store = AttachmentBlobStore(root, path_resolver=catalog.resolver, clock=lambda: clock)
        try:
            result = asyncio.run(store.collect_garbage(leases=ExternalResourceLeaseLedger(
                state_path=root / ".boxteam/resources.json",
            )))
            print(json.dumps({"collected": result}), flush=True)
        finally:
            store.close()
            catalog.close()
    """)
    artifacts = TestRunContext.from_test_file(Path(request.node.path)).prepare().artifacts_dir
    prepare = store._catalog.prepare_fork_attachment_claims
    collections: list[list[str]] = []

    def collect_at_claim(**kwargs: object) -> None:
        # 只固定真实 claim 提交前/后的时序，另一进程沿原 GC 入口执行，不手填业务状态。
        if not gc_first:
            prepare(**kwargs)
        command = [sys.executable, "-c", program, str(container.workspace_root), original.gc_after]
        outcome = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        prefix = f"{creation_kind}-{'gc-first' if gc_first else 'fork-first'}"
        (artifacts / f"{prefix}.stdout.raw").write_text(outcome.stdout, encoding="utf-8")
        (artifacts / f"{prefix}.stderr.raw").write_text(outcome.stderr, encoding="utf-8")
        (artifacts / f"{prefix}.json").write_text(json.dumps({
            "command": command, "exit_code": outcome.returncode,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert outcome.returncode == 0, (outcome.stdout, outcome.stderr)
        collections.append(json.loads(outcome.stdout)["collected"])
        assert collections == ([[original.attachment_id]] if gc_first else [[]])
        assert directory.exists() is not gc_first
        if gc_first:
            prepare(**kwargs)

    monkeypatch.setattr(store._catalog, "prepare_fork_attachment_claims", collect_at_claim)
    if gc_first:
        with pytest.raises(FileNotFoundError):
            await context.forks.fork(fork_request)
        capture = context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
        assert capture.state == "aborted"
        assert [row[0] for row in store._catalog.connection.execute(
            "SELECT state FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (key,),
        )] == ["aborted"]
        snapshot, = store._catalog.fork_attachment_snapshots(
            key, target_session_id=capture.target_session_id, target_thread_id=capture.target_thread_id,
        )
        assert (snapshot.source_attachment_id, snapshot.target_attachment_id) == (
            original.attachment_id, original.attachment_id,
        )
        if creation_kind == "new_session_main":
            assert context.sessions.path_resolver.catalog_store.get_creation_record(key).state == "aborted"
            with pytest.raises(KeyError):
                context.sessions.path_resolver.catalog_store.get_node(capture.target_session_id)
        else:
            with context.forks._threads.for_owner_session(parent.session_id) as threads:
                record = threads.control_store.get_thread_creation_record(key)
                assert record.state == "aborted"
                root = context.sessions.path_resolver.resolve_session_node(parent.session_id)
                assert not (root / record.final_relative_locator).exists()
                assert not (root / record.staging_locator).exists()
        final = store._catalog.get_directory(original.attachment_id)
        assert final.availability == "tombstoned"
        assert (final.gc_after, final.relative_locator) == (original.gc_after, original.relative_locator)
        with pytest.raises(RuntimeError, match="已中止"):
            await context.forks.fork(fork_request)
    else:
        result = await context.forks.fork(fork_request)
        resource, = result.resources
        assert store.read(result.session.session_id, resource.target_uri, owner_thread_id=result.thread_id).data == "冷恢复附件正文".encode()
        assert store._catalog.get_directory(original.attachment_id).gc_after is None
        assert await store.collect_garbage(leases=container.external_resource_leases) == ()
        assert [row[0] for row in store._catalog.connection.execute(
            "SELECT state FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (key,),
        )] == ["committed"]
    assert len(collections) == 1
    assert canonical.read_bytes() == original_history


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


async def _assert_unpublished_fork_rollback(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    creation_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> str:
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
        with context.forks._threads.for_owner_session(parent.session_id) as threads:
            record = threads.control_store.get_thread_creation_record(key)
            assert record.state == "aborted"
            session_root = resolver.resolve_session_node(parent.session_id)
            assert not (session_root / record.final_relative_locator).exists()
            assert not (session_root / record.staging_locator).exists()
    return claim[1]


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
async def test_unpublished_child_fork_rollback(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单独验证真实执行生成的深层文件在 child 失败回滚后全部清除。"""
    await _assert_unpublished_fork_rollback(
        production_fork_container, fork_execution_source, "new_child_thread", monkeypatch,
    )


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_unpublished_fork_failure_releases_only_its_original_directory_claim(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    creation_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await _assert_unpublished_fork_rollback(
        production_fork_container, fork_execution_source, creation_kind, monkeypatch,
    )
    container = production_fork_container
    store = container.attachment_blob_store
    resolver = _production_fork_context(container).sessions.path_resolver
    # 不修写业务记录；原删除 owner 释放最后关联后，原 GC 不得被失败 claim 无限阻塞。
    now = datetime.now(UTC)
    monkeypatch.setattr(store, "_clock", lambda: now)
    await resolver.delete_subtree(idempotency_key="delete-failed-fork-source", root_node_id=fork_execution_source.session_id)
    monkeypatch.setattr(store, "_clock", lambda: now + timedelta(days=31))
    assert await store.collect_garbage(leases=container.external_resource_leases) == (identity,)
    assert store._catalog.get_directory(identity).availability == "tombstoned"


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_remote_import_failure_settles_both_owners_without_publishing_target(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    creation_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = production_fork_container
    target, transport = remote_fork_target
    for container in (source, target):
        for owner in (container.terminal_resource_owner, container.browser_resource_owner):
            monkeypatch.setattr(owner._transport, "list_resources", AsyncMock(return_value=[]))
    source_thread = source.session_service.path_resolver.main_thread_id(fork_execution_source.session_id)
    source_items = source.checkpointer._storage.storage.read_items(fork_execution_source.session_id, source_thread)
    reference, = [ref for item in source_items if isinstance(item.payload, dict) for ref in item.payload.get("attachments", [])]
    source_token = set_current_gateway_id("gateway_4aae160f813ed87bcf65a8bb8bfd3729")
    try:
        request = await _fork_request(_production_fork_context(source), fork_execution_source.session_id,
            creation_kind=creation_kind, mode="full_rollout_copy")
    finally:
        reset_current_gateway_id(source_token)
    target_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    try:
        parent = await target.session_service.create(SessionCreateRequest(title="跨端失败容器"))
        request = request.model_copy(update={
            "source_uri": thread_resource_uri(source.workspace_id, fork_execution_source.session_id, source_thread,
                gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729"),
            "target_container_uri": resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="session",
                tail_segments=(parent.session_id,) if creation_kind == "new_child_thread" else (),
                gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744"),
            "resources": [SessionForkResourceChoice(resource_uri=reference["file_id"], action="copy_snapshot")],
        })
        key = target.session_context_fork_service._creation_key(request.idempotency_key)
        observer = fork_import.observe_phase
        phases: list[str] = []

        async def fail_after_import(operation_id: str, phase: str, **kwargs: object) -> None:
            await observer(operation_id, phase, **kwargs)
            if operation_id == key and phase == "copy.attachments.import_staged":
                phases.append(phase)
                raise RuntimeError("injected-after-frozen-directory-import")

        with monkeypatch.context() as failure:
            failure.setattr(fork_import, "observe_phase", fail_after_import)
            with pytest.raises(RuntimeError, match="injected-after-frozen-directory-import"):
                await target.session_context_fork_service.fork(request)
        assert phases == ["copy.attachments.import_staged"]
        catalog = target.session_service.path_resolver.catalog_store
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        assert capture.state == "aborted"
        assert catalog.get_fork_export_release(key)[0] == "confirmed"
        source_capture = source.session_service.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
        assert source_capture.state == "aborted"
        assert [tuple(row) for row in source.attachment_blob_store._catalog.connection.execute(
            "SELECT export_released FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (key,),
        )] == [(1,)]
        state, identity = target.attachment_blob_store._catalog.connection.execute(
            "SELECT state,target_attachment_id FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (key,),
        ).fetchone()
        assert state == "aborted"
        if creation_kind == "new_session_main":
            assert catalog.get_creation_record(key).state == "aborted"
            with pytest.raises(KeyError):
                catalog.get_node(capture.target_session_id)
        else:
            with target.session_context_fork_service._threads.for_owner_session(parent.session_id) as threads:
                record = threads.control_store.get_thread_creation_record(key)
                assert record.state == "aborted"
                root = target.session_service.path_resolver.resolve_session_node(parent.session_id)
                assert not (root / record.final_relative_locator).exists()
                assert not (root / record.staging_locator).exists()
        store = target.attachment_blob_store
        directory = store._catalog.get_directory(identity)
        assert directory.unattached_since is not None and directory.gc_after is not None
        assert store._catalog.connection.execute(
            "SELECT count(*) FROM attachment_directory_associations WHERE attachment_id=? AND state='active'", (identity,),
        ).fetchone()[0] == 0
        assert await store.collect_garbage(leases=target.external_resource_leases) == ()
        monkeypatch.setattr(store, "_clock", lambda: datetime.fromisoformat(directory.gc_after) + timedelta(seconds=1))
        assert await store.collect_garbage(leases=target.external_resource_leases) == (identity,)
        assert store._catalog.get_directory(identity).availability == "tombstoned"
        calls = len(transport.calls)
        with pytest.raises(RuntimeError, match="已中止"):
            await target.session_context_fork_service.fork(request)
        assert len(transport.calls) == calls
        assert source.checkpointer._storage.storage.read_items(fork_execution_source.session_id, source_thread) == source_items
    finally:
        reset_current_gateway_id(target_token)
