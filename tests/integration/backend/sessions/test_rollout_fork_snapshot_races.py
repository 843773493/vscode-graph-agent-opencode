"""公开 fork 与来源真实追加、rewind 竞争时只消费同一冻结修订。"""

from __future__ import annotations

import asyncio

import pytest

from app.core import session_subtree_delete, thread_creation
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_context.checkpoint import fork_owner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
)
from tests.integration.backend.sessions import test_rollout_fork_modes as fork_cases
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_context as fork_context,  # noqa: PLC0414 - 复用真实生产 owner fixture
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414 - 请求身份 fixture
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_provider_prepare as fork_provider_prepare,  # noqa: PLC0414 - 真实封存 fixture
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414 - 隔离工作区 owner
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    tool_set as tool_set,  # noqa: PLC0414 - 编译 fixture 依赖
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("mutation", ["append", "rewind"])
@pytest.mark.parametrize("captured", [False, True], ids=["before-capture", "after-capture"])
async def test_public_fork_source_change_uses_exact_snapshot_or_rejects(
    fork_context, fork_provider_prepare, monkeypatch: pytest.MonkeyPatch,
    mode, creation_kind, mutation, captured,
) -> None:
    source = await fork_cases._seed_source(fork_context, fork_provider_prepare)
    saver = fork_context.saver
    storage = saver._storage.storage
    owner = saver.forks.source.resolve_main_thread_owner(source.session_id)
    request = await fork_cases._fork_request(
        fork_context, source.session_id, creation_kind=creation_kind, mode=mode,
    )
    original_items = {
        item.item_id: item.to_dict()
        for item in storage.read_items(source.session_id, owner.thread_id)
    }
    original_jsonl = storage.jsonl_path(source.session_id, thread_id=owner.thread_id).read_bytes()
    with storage.read_connection(source.session_id, owner.thread_id, "") as connection:
        visible_sql = (
            "SELECT item_id FROM context_view_items WHERE view_id=? "
            "AND visible=1 AND context_selected=1"
        )
        parameters = [request.source_view_id]
        if mode != "full_rollout_copy":
            visible_sql += (
                " AND context_ordinal<=(SELECT context_ordinal FROM context_view_items "
                "WHERE view_id=? AND item_id=?)"
            )
            parameters.extend((request.source_view_id, request.anchor_item_id))
        selected_ids = [row[0] for row in connection.execute(
            visible_sql + " ORDER BY context_ordinal", parameters,
        )]
        assert selected_ids
    source_checkpoint = await fork_cases._get_tuple(fork_context, source.session_id)
    assert source_checkpoint is not None
    control = fork_context.forks._threads.for_owner_session(source.session_id).control_store
    original_children = control.list_child_thread_rows()
    operation_key = fork_context.forks._creation_key(request.idempotency_key)
    phase = "copy.source.before_captured_cas" if captured else "copy.source.local_capture_authorized"
    reached, release = asyncio.Event(), asyncio.Event()
    original_observer = fork_owner.observe_phase

    async def pause_capture(key: str, observed_phase: str, **kwargs):
        await original_observer(key, observed_phase, **kwargs)
        if key == operation_key and observed_phase == phase:
            reached.set()
            await release.wait()

    monkeypatch.setattr(fork_owner, "observe_phase", pause_capture)
    operation = asyncio.create_task(fork_context.forks.fork(request))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        assert not operation.done()

        def change_source() -> None:
            if mutation == "append":
                saver.accept_turn(
                    source.session_id, thread_id=owner.thread_id,
                    accepted_ingress_id="snapshot-race-input",
                    acceptance_idempotency_key="snapshot-race-accept",
                    job_id="snapshot-race-job", admission_idempotency_key="snapshot-race-admission",
                    payload="冻结后追加的真实输入", root_item_id="snapshot-race-root",
                )
            else:
                with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
                    saver.rewind(source_checkpoint.config, checkpoint_id="cp2")

        # 在 fork 暂停期间完成真实 owner mutation，证明没有继续持有来源读锁。
        await asyncio.wait_for(asyncio.to_thread(change_source), timeout=10)
        changed = saver.load_history(source.session_id, owner.thread_id, TurnHistoryLoadRequest())
        assert changed.source_view_revision > request.source_view_revision
        if mutation == "append":
            assert "snapshot-race-root" in {
                item.item_id for item in storage.read_items(source.session_id, owner.thread_id)
            }
        else:
            assert changed.source_view_id != request.source_view_id

        release.set()
        if not captured:
            with pytest.raises(ValueError, match="source-mismatch:"):
                await operation
            assert (await fork_context.sessions.list(limit=100)).total == 1
            assert control.list_child_thread_rows() == original_children
            capture = fork_context.sessions.path_resolver.catalog_store.get_fork_source_capture(
                copy_operation_id=operation_key,
            )
            assert capture is not None and capture.state == "aborted"
            return

        result = await operation
        target_session = result.session.session_id
        target_thread = result.thread_id
        assert (result.source_view_id, result.source_view_revision) == (
            request.source_view_id, request.source_view_revision,
        )
        copied = storage.read_items(target_session, target_thread)
        assert copied and all(item.to_dict() == original_items[item.item_id] for item in copied)
        assert "snapshot-race-root" not in {item.item_id for item in copied}
        with storage.read_connection(target_session, target_thread, "") as connection:
            target_view = connection.execute(
                "SELECT b.head_view_id FROM checkpoint_namespace_state n "
                "JOIN branches b ON b.branch_id=n.active_branch_id WHERE n.checkpoint_ns=''",
            ).fetchone()[0]
            assert [row[0] for row in connection.execute(
                "SELECT item_id FROM context_view_items WHERE view_id=? "
                "AND visible=1 AND context_selected=1 ORDER BY context_ordinal", (target_view,),
            )] == selected_ids
        target_jsonl = storage.jsonl_path(target_session, thread_id=target_thread).read_bytes()
        if mode == "full_rollout_copy":
            assert target_jsonl == original_jsonl
            assert {item.item_id for item in copied} == original_items.keys()
        else:
            assert all(line in original_jsonl.splitlines() for line in target_jsonl.splitlines())

        # 独立 Session 在删源、关闭 resident reader 后仍从自己的冻结正文读取。
        if creation_kind == "new_session_main":
            await fork_context.sessions.path_resolver.delete_subtree(
                idempotency_key="snapshot-race-delete-source", root_node_id=source.session_id,
            )
        with RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root) as cold:
            restored = cold._storage.storage.read_items(target_session, target_thread)
            assert [item.to_dict() for item in restored] == [item.to_dict() for item in copied]
            history = cold.load_history(target_session, target_thread, TurnHistoryLoadRequest())
            assert history.items
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_session_forks_share_gate_and_publish_distinct_idle_children(
    fork_context, fork_provider_prepare, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await fork_cases._seed_source(fork_context, fork_provider_prepare)
    owner = fork_context.saver.forks.source.resolve_main_thread_owner(source.session_id)
    storage = fork_context.saver._storage.storage
    original_jsonl = storage.jsonl_path(source.session_id, thread_id=owner.thread_id).read_bytes()
    first = await fork_cases._fork_request(
        fork_context, source.session_id, creation_kind="new_child_thread",
    )
    first = first.model_copy(update={"idempotency_key": "same-session-fork-1"})
    second = first.model_copy(update={"idempotency_key": "same-session-fork-2"})
    keys = {fork_context.forks._creation_key(item.idempotency_key) for item in (first, second)}
    admission_seen, gate_attempts, capture_seen = set(), set(), set()
    admissions, gate_waiters = asyncio.Event(), asyncio.Event()
    release_admissions, captures, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_admission = thread_creation.observe_phase
    original_capture = fork_owner.observe_phase

    async def observe_admission(key: str, phase: str, **kwargs):
        await original_admission(key, phase, **kwargs)
        if key in keys and phase == "thread.creation.before_admission":
            admission_seen.add(key)
            if admission_seen == keys:
                admissions.set()
            await release_admissions.wait()
            gate_attempts.add(key)
            if gate_attempts == keys:
                gate_waiters.set()

    async def pause_captures(key: str, phase: str, **kwargs):
        await original_capture(key, phase, **kwargs)
        if key in keys and phase == "copy.source.before_captured_cas":
            capture_seen.add(key)
            if capture_seen == keys:
                captures.set()
            await release.wait()

    monkeypatch.setattr(thread_creation, "observe_phase", observe_admission)
    monkeypatch.setattr(fork_owner, "observe_phase", pause_captures)
    control = fork_context.forks._threads.for_owner_session(source.session_id).control_store
    tasks = []
    try:
        # preflight 需要来源共享锁，先完成它再阻塞 child 的独占准入。
        tasks = [asyncio.create_task(fork_context.forks.fork(item)) for item in (first, second)]
        await asyncio.wait_for(admissions.wait(), timeout=10)
        # 同一把真实 Session gate 阻塞两条公开创建链，均不能提前登记 child。
        async with SessionLifecycleGate(storage.sessions_dir).exclusive(source.session_id):
            release_admissions.set()
            await asyncio.wait_for(gate_waiters.wait(), timeout=10)
            assert all(not task.done() for task in tasks)
            assert not control.list_child_thread_rows()
            for key in keys:
                with pytest.raises(KeyError):
                    control.get_thread_creation_record(key)
        await asyncio.wait_for(captures.wait(), timeout=15)
        assert all(not task.done() for task in tasks)
        assert not control.list_child_thread_rows()
        # source capture 返回后两条链均不再占用 Session gate，发布可各自重新准入。
        async with asyncio.timeout(5):
            async with SessionLifecycleGate(storage.sessions_dir).exclusive(source.session_id):
                assert len(control.list_pending_thread_creations()) == 2
        release.set()
        results = await asyncio.gather(*tasks)
        assert len({result.thread_id for result in results}) == 2
        assert {row.thread_id for row in control.list_child_thread_rows()} == {
            result.thread_id for result in results
        }
        assert not control.list_initial_execution_intents()
        for request, result in zip((first, second), results, strict=True):
            record = control.get_thread_creation_record(fork_context.forks._creation_key(request.idempotency_key))
            assert (record.state, record.purpose, record.initial_state, record.child_thread_id) == (
                "published", "fork", "idle", result.thread_id,
            )
            assert result.session.session_id == source.session_id
            assert fork_context.saver.load_history(source.session_id, result.thread_id, TurnHistoryLoadRequest()).items
        assert storage.jsonl_path(source.session_id, thread_id=owner.thread_id).read_bytes() == original_jsonl
        # 整个 Session 删除会同时清理两个 child，内部 fork 关系不能形成自阻塞。
        await fork_context.sessions.path_resolver.delete_subtree(
            idempotency_key="delete-session-with-sibling-forks", root_node_id=source.session_id,
        )
        assert (await fork_context.sessions.list(limit=100)).total == 0
    finally:
        release_admissions.set()
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("captured", [False, True], ids=["before-capture", "after-capture"])
async def test_same_session_deletion_cancels_pending_fork_without_gate_reentry(
    fork_context, fork_provider_prepare, monkeypatch: pytest.MonkeyPatch, captured,
) -> None:
    source = await fork_cases._seed_source(fork_context, fork_provider_prepare)
    request = await fork_cases._fork_request(
        fork_context, source.session_id, creation_kind="new_child_thread", mode="full_rollout_copy",
    )
    key = fork_context.forks._creation_key(request.idempotency_key)
    delete_key = "delete-session-during-fork"
    resolver = fork_context.sessions.path_resolver
    catalog = resolver.catalog_store
    control = fork_context.forks._threads.for_owner_session(source.session_id).control_store
    session_node = resolver.resolve_session_node(source.session_id)
    sessions_root = fork_context.saver._storage.storage.sessions_dir
    capture_phase = "copy.source.before_captured_cas" if captured else "copy.source.local_capture_authorized"
    reached, release = asyncio.Event(), asyncio.Event()
    draining, fenced, release_delete = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_capture = fork_owner.observe_phase
    original_deletion = session_subtree_delete.observe_phase

    async def pause_capture(operation_key: str, phase: str, **kwargs):
        await original_capture(operation_key, phase, **kwargs)
        if operation_key == key and phase == capture_phase:
            reached.set()
            await release.wait()

    async def observe_deletion(operation_key: str, phase: str, **kwargs):
        await original_deletion(operation_key, phase, **kwargs)
        if operation_key != delete_key:
            return
        if phase == "deletion.drain_waiting/" + source.session_id:
            draining.set()
        elif phase == "deletion.fence_closed/" + source.session_id:
            # 留住已排空的原控制库，先核对创建 owner 的持久中止，再允许物理删除。
            fenced.set()
            await release_delete.wait()

    monkeypatch.setattr(fork_owner, "observe_phase", pause_capture)
    monkeypatch.setattr(session_subtree_delete, "observe_phase", observe_deletion)
    operation = asyncio.create_task(fork_context.forks.fork(request))
    deletion = None
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        preparing = control.get_thread_creation_record(key)
        assert preparing.state == "preparing"
        assert not control.list_child_thread_rows()
        deletion = asyncio.create_task(resolver.delete_subtree(
            idempotency_key=delete_key, root_node_id=source.session_id,
        ))
        await asyncio.wait_for(draining.wait(), timeout=10)
        assert catalog.get_node(source.session_id).state == "deleting"
        assert not deletion.done() and not fenced.is_set()
        # 删除正在等待原创建 holder；不能占着原操作清理所需的 Session gate。
        async with asyncio.timeout(5):
            async with SessionLifecycleGate(sessions_root).exclusive(source.session_id):
                assert control.get_thread_creation_record(key) == preparing
        release.set()
        with pytest.raises(SessionDeletionPendingError, match="session_deletion_pending:"):
            await operation
        await asyncio.wait_for(fenced.wait(), timeout=10)
        aborted = control.get_thread_creation_record(key)
        assert (aborted.state, aborted.child_thread_id, aborted.preimage_hash) == (
            "aborted", preparing.child_thread_id, preparing.preimage_hash,
        )
        assert not control.list_child_thread_rows()
        assert not control.list_initial_execution_intents()
        for locator in (aborted.staging_locator, aborted.final_relative_locator):
            assert not (session_node / locator).exists()
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        assert capture is not None and capture.state == "aborted"
        assert not capture_directory(sessions_root, capture).exists()
        release_delete.set()
        await asyncio.wait_for(deletion, timeout=10)
        assert (await fork_context.sessions.list(limit=100)).total == 0
        assert not session_node.exists()
        assert catalog.get_fork_source_capture(copy_operation_id=key) == capture
    finally:
        release.set()
        release_delete.set()
        tasks = [operation] if deletion is None else [operation, deletion]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
