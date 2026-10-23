"""真实 pinned fork 与整树删除在 catalog 提交前后中断，只服从持久 winner。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.api.deps import get_session_context_fork_service, verify_local_token
from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.path_utils import workspace_context
from app.core.session_catalog_store.contracts import (
    SourceRetainedByForkError,
    SourceRetentionOperationPendingError,
)
from app.core.session_control_store import SessionControlStore
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import SessionCreateRequest, SessionDTO
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
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


class CatalogCommitInterrupted(BaseException):
    """跳过业务补偿，分别留下真实 rollback 或 commit 后的事实。"""


@pytest.mark.parametrize("first", ["claim", "delete"])
@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
async def test_fork_retention_and_subtree_delete_recover_catalog_commit_winner(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
    first: str, committed: bool,
) -> None:
    container = production_fork_container
    source = fork_execution_source
    context = _production_fork_context(container)
    resolver = context.sessions.path_resolver
    catalog = resolver.catalog_store
    # 导航树也由唯一 operation worker 和 Session 创建入口产生。
    operations = container.session_catalog_service.operations
    await operations.start()
    try:
        folder = await container.session_catalog_service.ensure_folder_path(["竞争删除的整树"])
        assert folder is not None
        scope = local_navigation_scope(container.workspace_id)
        operation_id = create_prefixed_id("op")
        await operations.enqueue(NavigationMutationEnqueueRequest(intents=[NavigationMutationIntentDTO(
            client_operation_id=operation_id, client_sequence=1, kind="move_node",
            base_catalog_revision=resolver.revision, expected_revision=catalog.get_node(source.session_id).revision,
            target_node_id=source.session_id, parent_node_id=folder,
        )]), scope)
        assert (await operations.await_terminal(operation_id, scope)).state == "committed"
        sibling = await context.sessions.create(SessionCreateRequest(title="同树另一会话", folder_id=folder))
    finally:
        await operations.stop()
    node_ids = (folder, source.session_id, sibling.session_id)
    before_nodes = {node_id: catalog.get_node(node_id) for node_id in node_ids}
    directories = {session_id: resolver.resolve_session_node(session_id)
                   for session_id in (source.session_id, sibling.session_id)}
    history_path = (directories[source.session_id] / "threads" /
                    resolver.main_thread_id(source.session_id) / "rollout/rollout.jsonl")
    source_history = history_path.read_bytes()
    fork_request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", relationship="pinned",
    )
    key = context.forks._creation_key(fork_request.idempotency_key)
    delete_key = "delete-retention-commit-winner"
    original_transaction = catalog.write_transaction
    interrupted = []

    def observed_state(connection):
        if first == "claim":
            return tuple(connection.execute(
                "SELECT fork_retention_claim_id,state FROM fork_retention_claims ORDER BY fork_retention_claim_id",
            ))
        return tuple(connection.execute(
            "SELECT state FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", (delete_key,),
        ))

    @contextmanager
    def interrupt_transaction():
        hit = False
        with original_transaction() as connection:
            before = observed_state(connection)
            yield connection
            after = observed_state(connection)
            hit = before != after and (first == "claim" or after[0][0] == "deleting")
            if hit:
                # 另一个真实 SQLite 连接在提交前仍看到完整 active 子树。
                with closing(sqlite3.connect(catalog.database_path)) as independent:
                    assert independent.execute(
                        "SELECT count(*) FROM nodes WHERE node_id IN (?,?,?) AND state='active'", node_ids,
                    ).fetchone()[0] == len(node_ids)
                interrupted.append("after-commit" if committed else "before-commit")
                if not committed:
                    raise CatalogCommitInterrupted(interrupted[-1])
        if hit:
            raise CatalogCommitInterrupted(interrupted[-1])

    with monkeypatch.context() as interruption:
        interruption.setattr(catalog, "write_transaction", interrupt_transaction)
        with pytest.raises(CatalogCommitInterrupted):
            if first == "claim":
                await context.forks.fork(fork_request)
            else:
                await resolver.delete_subtree(idempotency_key=delete_key, root_node_id=folder)
    assert interrupted == ["after-commit" if committed else "before-commit"]
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    creation = catalog.get_creation_record(key) if first == "claim" else None
    if first == "claim":
        assert capture is not None and capture.state == "preparing"
        assert creation is not None and creation.state == "preparing"
        claims = catalog.list_pinned_claims_for_source(source.session_id)
        assert [(c.fork_retention_claim_id, c.state) for c in claims] == (
            [(capture.fork_id, "preparing")] if committed else []
        )
    else:
        assert capture is None
        delete_record = catalog.get_subtree_delete_record(delete_key)
        assert delete_record.state == ("deleting" if committed else "preparing")
        assert set(delete_record.frozen_session_locators) == set(directories)
        assert delete_record.drained_session_ids == ()
    for directory in directories.values():
        with closing(SessionControlStore(directory / "session-control.sqlite", read_only=True)) as control:
            assert control.get_fence() == ("active", 1)
            assert control.list_non_terminal_leases() == ()
    assert {catalog.get_node(node_id).state for node_id in node_ids} == (
        {"deleting"} if first == "delete" and committed else {"active"}
    )
    # 关闭旧连接并完整重建容器；后续操作不得借用旧 owner 的内存状态。
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch, request)
    recovered = _production_fork_context(restarted)
    resolver = recovered.sessions.path_resolver
    catalog = resolver.catalog_store
    for kind in ("terminal", "browser"):
        provider = restarted.session_resource_service._provider_registry.get(kind)
        monkeypatch.setattr(provider._owner._transport, "list_resources", AsyncMock(return_value=[]))
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[verify_local_token] = lambda: "local-dev-token"
    app.dependency_overrides[get_session_context_fork_service] = lambda: recovered.forks

    async def retry_fork():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace") as client:
            response = await client.post(
                f"/api/v1/sessions/{source.session_id}/fork-context", json=fork_request.model_dump(mode="json"),
                headers={"X-Request-ID": f"retention-{first}-{committed}",
                         "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84"},
            )
            assert response.headers["X-Request-ID"] == f"retention-{first}-{committed}"
            return response

    # 回滚的一方没有 winner；让另一方先提交，检验迟到恢复服从已 durable 的结果。
    fork_wins = (first == "claim") == committed
    with workspace_context(restarted.workspace_id, restarted.workspace_root, session_catalog=restarted.session_catalog):
        if fork_wins:
            if first == "claim":
                with pytest.raises(SourceRetentionOperationPendingError) as blocked:
                    await resolver.delete_subtree(idempotency_key=delete_key, root_node_id=folder)
                assert blocked.value.claim_id == capture.fork_id
                assert blocked.value.source_session_id == source.session_id
                assert blocked.value.target_session_id == creation.session_id
            response = await retry_fork()
            assert response.status_code == 200, response.text
            result = response.json()["data"]
            frozen = catalog.get_creation_record(key)
            current_capture = catalog.get_fork_source_capture(copy_operation_id=key)
            assert frozen.state == "published" and current_capture.state == "materialized"
            if creation is not None:
                assert (frozen.session_id, frozen.main_thread_id, frozen.preimage_hash, frozen.request_intent_json) == (
                    creation.session_id, creation.main_thread_id, creation.preimage_hash, creation.request_intent_json,
                )
                assert current_capture.fork_id == capture.fork_id
            assert (result["session"]["session_id"], result["thread_id"]) == (frozen.session_id, frozen.main_thread_id)
            claim, = catalog.list_pinned_claims_for_source(source.session_id)
            assert (claim.fork_retention_claim_id, claim.state, claim.target_session_id) == (
                current_capture.fork_id, "active", frozen.session_id,
            )
            if first == "delete":
                with pytest.raises(SourceRetainedByForkError) as blocked:
                    await resolver.delete_subtree(idempotency_key=delete_key, root_node_id=folder)
                assert blocked.value.claim_id == claim.fork_retention_claim_id
            assert catalog.get_subtree_delete_record(delete_key).state == "aborted"
            assert {node_id: catalog.get_node(node_id) for node_id in node_ids} == before_nodes
            assert history_path.read_bytes() == source_history
        else:
            if first == "claim":
                await resolver.delete_subtree(idempotency_key=delete_key, root_node_id=folder)
            response = await retry_fork()
            assert response.status_code == (404 if first == "claim" else 409), response.text
            if first == "delete":
                assert catalog.get_fork_source_capture(copy_operation_id=key) is None
                with pytest.raises(KeyError):
                    catalog.get_creation_record(key)
                results = await resolver.recover_pending_subtree_deletes()
                assert len(results) == 1 and results[0].record_state == "completed"
            else:
                assert catalog.get_creation_record(key).state == "aborted"
                assert catalog.get_fork_source_capture(copy_operation_id=key).state == "aborted"
            record = catalog.get_subtree_delete_record(delete_key)
            assert record.state == "completed" and set(record.drained_session_ids) == set(directories)
            for node_id in node_ids:
                with pytest.raises(KeyError):
                    catalog.get_node(node_id)
            assert catalog.list_pinned_claims_for_source(source.session_id) == []
            for session_id, directory in directories.items():
                assert not directory.exists()
                isolated = resolver.sessions_root / ".deleting" / delete_key / session_id
                with closing(SessionControlStore(isolated / "session-control.sqlite", read_only=True)) as control:
                    assert control.get_fence() == ("deleting", 2)
                    assert control.list_non_terminal_leases() == ()
            assert (resolver.sessions_root / ".deleting" / delete_key / source.session_id /
                    history_path.relative_to(directories[source.session_id])).read_bytes() == source_history
    artifacts = TestRunContext.from_test_file(Path(__file__)).prepare().artifacts_dir
    (artifacts / f"{first}-{committed}-{source.session_id}.json").write_text(json.dumps({
        "first": first, "interruption": interrupted[0], "fork_wins": fork_wins,
        "subtree": node_ids, "delete_state": catalog.get_subtree_delete_record(delete_key).state,
        "fork_http_status": response.status_code,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
