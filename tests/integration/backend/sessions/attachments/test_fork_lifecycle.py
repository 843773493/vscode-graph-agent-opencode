"""由真实执行生成附件历史，验证 fork 发布和失败的目录关联结算。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock

import pytest

from app.container import AppContainer
from app.core.resource_uri import (
    file_uri_child,
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
from app.schemas.internal_v2.text_mutation import CreateText, PatchText
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
@pytest.mark.parametrize("release_first", [True, False], ids=["release-first", "fork-first"])
async def test_directory_claim_and_release_share_holder_before_target_publication(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    creation_kind: str, release_first: bool, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
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
    identity, = store._catalog.connection.execute("SELECT attachment_id FROM attachment_directories").fetchone()
    original = store._catalog.get_directory(identity)
    assert original is not None and original.availability == "released"
    directory = resolve_attachment_directory(store._attachments_root, original.relative_locator)
    assert not directory.exists()
    assert store.read(source.session_id, store.reference(identity)).data == "冷恢复附件正文".encode()
    assert store._catalog.get_directory(identity).release_pending == 0
    parent = await context.sessions.create(SessionCreateRequest(title="释放竞争的副本容器"))
    fork_request = await _fork_request(
        context, source.session_id, mode="full_rollout_copy", creation_kind=creation_kind,
        target_container_uri=parent.uri if creation_kind == "new_child_thread" else None,
    )
    key = context.forks._creation_key(fork_request.idempotency_key)
    # 真实条件修改再显式删除回到基线；原 holder 保护交接，竞争从持久待处理事实开始。
    with store.directory_access(identity):
        temporary = file_uri_child(store.directory_uri(identity), "derived", "handoff.txt")
        created = container.text_mutations.execute(CreateText(target=temporary, content="temporary", expected_version="absent"))
        assert created["status"] == "success"
        deleted = container.text_mutations.execute(PatchText(
            target=store.directory_uri(identity), input="*** Begin Patch\n*** Delete File: derived/handoff.txt\n*** End Patch",
            expected_versions={"derived/handoff.txt": "sha256:" + hashlib.sha256(b"temporary").hexdigest()},
            explanation="显式恢复初始基线，验证 fork 与工作副本释放的原 holder 交接",
        ))
        assert deleted["status"] == "success"
    assert store._catalog.get_directory(identity).release_pending == 1
    program = dedent("""\
        import asyncio
        import json
        import sys
        from pathlib import Path
        from app.core.path_utils import build_session_catalog_components
        from app.core.workspace_identity import read_workspace_id
        from app.services.infrastructure.attachment_blob_catalog.store import AttachmentBlobStore
        from app.services.infrastructure.external_resource_leases import ExternalResourceLeaseLedger
        root = Path(sys.argv[1])
        catalog = build_session_catalog_components(root / ".boxteam/sessions", read_workspace_id(root))
        store = AttachmentBlobStore(root, path_resolver=catalog.resolver,
            leases=ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json"))
        try:
            result = asyncio.run(store.process_pending_directories())
            print(json.dumps({"released": result}), flush=True)
        finally:
            store.close()
            catalog.close()
    """)
    artifacts = TestRunContext.from_test_file(Path(request.node.path)).prepare().artifacts_dir
    releases: list[list[str]] = []

    def release_in_process() -> None:
        command = ["timeout", "30", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
                   sys.executable, "-c", program, str(container.workspace_root)]
        outcome = subprocess.run(command, capture_output=True, text=True, timeout=35, check=False)
        prefix = f"{creation_kind}-{'release-first' if release_first else 'fork-first'}"
        (artifacts / f"{prefix}.stdout.raw").write_text(outcome.stdout, encoding="utf-8")
        (artifacts / f"{prefix}.stderr.raw").write_text(outcome.stderr, encoding="utf-8")
        (artifacts / f"{prefix}.json").write_text(json.dumps({
            "command": command, "exit_code": outcome.returncode,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert outcome.returncode == 0, (outcome.stdout, outcome.stderr)
        releases.append(json.loads(outcome.stdout)["released"])
        assert releases == ([[identity]] if release_first else [[]])
        assert directory.exists() is not release_first

    if release_first:
        release_in_process()
    else:
        prepare = store._catalog.prepare_fork_attachment_claims

        def release_at_claim(**kwargs: object) -> None:
            # 原 holder 覆盖 manifest 到 claim 的窗口，另一进程不得提前释放目录。
            release_in_process()
            prepare(**kwargs)

        monkeypatch.setattr(store._catalog, "prepare_fork_attachment_claims", release_at_claim)
    result = await context.forks.fork(fork_request)
    resource, = result.resources
    assert store.read(result.session.session_id, resource.target_uri, owner_thread_id=result.thread_id).data == "冷恢复附件正文".encode()
    final = store._catalog.get_directory(identity)
    assert final.relative_locator == original.relative_locator and final.release_pending == 0
    assert final.availability == "available" and await store.process_pending_directories() == ()
    assert [row[0] for row in store._catalog.connection.execute(
        "SELECT state FROM attachment_directory_fork_claims WHERE copy_operation_id=?", (key,),
    )] == ["committed"]
    assert len(releases) == 1 and canonical.read_bytes() == original_history


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
    assert store._catalog.get_directory(identity).release_pending == 0
    assert await store.process_pending_directories() == ()
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
    assert record.release_pending == 0 and record.availability == "released"
    directory = resolve_attachment_directory(store._attachments_root, record.relative_locator)
    assert not directory.exists()
    assert await store.process_pending_directories() == ()
    reopened = await container.workspace_service.get_file_content(path=store.reference(identity))
    assert reopened.content == "冷恢复附件正文"


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
    # 原删除 owner 释放最后关联后，失败 claim 不得无限阻塞工作副本释放。
    await resolver.delete_subtree(idempotency_key="delete-failed-fork-source", root_node_id=fork_execution_source.session_id)
    assert await store.process_pending_directories() == ()
    assert store._catalog.get_directory(identity).availability == "released"


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
        assert directory.release_pending == 0 and directory.availability == "released"
        assert store._catalog.connection.execute(
            "SELECT count(*) FROM attachment_directory_associations WHERE attachment_id=? AND state='active'", (identity,),
        ).fetchone()[0] == 0
        assert await store.process_pending_directories() == ()
        assert store._catalog.get_directory(identity).availability == "released"
        calls = len(transport.calls)
        with pytest.raises(RuntimeError, match="已中止"):
            await target.session_context_fork_service.fork(request)
        assert len(transport.calls) == calls
        assert source.checkpointer._storage.storage.read_items(fork_execution_source.session_id, source_thread) == source_items
    finally:
        reset_current_gateway_id(target_token)
