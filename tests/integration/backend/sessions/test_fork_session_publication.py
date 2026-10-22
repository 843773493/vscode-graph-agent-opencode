"""真实执行来源的附件预留、Session 发布与冷恢复共享同一冻结创建意图。"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app import main
from app.container import AppContainer
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.copy_attachment_settlement import (
    read_copy_attachment_settlements,
)
from app.schemas.internal_v2.session import SessionDTO
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
    _isolate_non_navigation_services,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _main_thread_node,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)


class PublicationInterrupted(BaseException):
    """绕过业务异常补偿，保留中断点已经提交的持久事实。"""


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("window", [
    "renamed", "catalog_committed", "settlement_published", "claims_committed",
])
async def test_copy_session_publication_recovers_original_attachment_settlement(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, window: str,
) -> None:
    container = production_fork_container
    context = _production_fork_context(container)
    source = fork_execution_source
    source_jsonl = (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes()
    fork_request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    )
    key = context.forks._creation_key(fork_request.idempotency_key)
    catalog = context.sessions.path_resolver.catalog_store
    attachments = container.attachment_blob_store
    stages = []

    def settlement(creation):
        resolver = context.sessions.path_resolver
        directory = (resolver.resolve_prepared_session_node(creation) if creation.state == "preparing"
                     else resolver.resolve_session_node(creation.session_id))
        with closing(SessionControlStore(directory / "session-control.sqlite", read_only=True)) as control:
            records = read_copy_attachment_settlements(control)
            assert len(records) == 1
            record = records[0]
            assert (record.copy_operation_id, record.target_session_id, record.target_thread_id,
                    record.publication_preimage_hash) == (
                key, creation.session_id, creation.main_thread_id, creation.preimage_hash,
            )
            capture = catalog.get_fork_source_capture(copy_operation_id=key)
            assert capture is not None and record.preimage_hash == capture.preimage_hash
            assert record.claim_ids == attachments._catalog.fork_attachment_claim_ids(key)
            lease = control.get_lease(record.lease_id)
            assert (lease.fencing_token, lease.captured_lifecycle_generation) == (
                record.lease_fencing_token, record.captured_generation,
            )
            return record, lease

    rename = context.sessions._creation_service._rename_staging_to_target
    mark_published = attachments.mark_copy_published
    finalize = attachments.finalize_copy_attachments

    def rename_observed(creation, metadata):
        # 真实附件 owner 必须在 catalog 发布前登记 settlement 及原 lease。
        record, lease = settlement(creation)
        assert record.state == "preparing" and lease.state == "active"
        assert catalog.get_creation_record(key).state == "preparing"
        with pytest.raises(KeyError):
            catalog.get_node(creation.session_id)
        rename(creation, metadata)
        stages.append("renamed")
        if window == "renamed":
            raise PublicationInterrupted(window)

    def mark_observed(capture, *, publication_preimage_hash):
        creation = catalog.get_creation_record(key)
        assert creation.state == "published"
        assert catalog.get_node(creation.session_id).main_thread_id == creation.main_thread_id
        assert settlement(creation)[0].state == "preparing"
        stages.append("catalog_committed")
        if window == "catalog_committed":
            raise PublicationInterrupted(window)
        mark_published(capture, publication_preimage_hash=publication_preimage_hash)
        record, lease = settlement(creation)
        assert record.state == "published_pending_attachment_commit" and lease.state == "active"
        stages.append("settlement_published")
        if window == "settlement_published":
            raise PublicationInterrupted(window)

    async def finalize_observed(capture, *, publication_preimage_hash):
        await finalize(capture, publication_preimage_hash=publication_preimage_hash)
        record, lease = settlement(catalog.get_creation_record(key))
        assert record.state == "committed" and lease.state == "completed"
        stages.append("claims_committed")
        raise PublicationInterrupted(window)

    with monkeypatch.context() as interruption:
        interruption.setattr(context.sessions._creation_service, "_rename_staging_to_target", rename_observed)
        interruption.setattr(attachments, "mark_copy_published", mark_observed)
        interruption.setattr(attachments, "finalize_copy_attachments", finalize_observed)
        with pytest.raises(PublicationInterrupted, match=window):
            await context.forks.fork(fork_request)
    frozen = catalog.get_creation_record(key)
    reserved, old_lease = settlement(frozen)
    assert stages[-1] == window
    assert frozen.state == ("preparing" if window == "renamed" else "published")
    assert reserved.state == {
        "renamed": "preparing", "catalog_committed": "preparing",
        "settlement_published": "published_pending_attachment_commit", "claims_committed": "committed",
    }[window]
    resolver = context.sessions.path_resolver
    target_root = (resolver.resolve_prepared_session_node(frozen) if frozen.state == "preparing"
                   else resolver.resolve_session_node(frozen.session_id))
    assert target_root.is_dir() and not (context.sessions.path_resolver.sessions_root / ".staging" / frozen.session_id).exists()
    frozen_bytes = (target_root / "threads" / frozen.main_thread_id / "rollout/rollout.jsonl").read_bytes()
    assert frozen_bytes == source_jsonl
    # 替换完整容器，恢复不能借用原 owner 的内存状态；不手工推进任一业务记录。
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch, request)
    _isolate_non_navigation_services(restarted, monkeypatch)
    recovered = _production_fork_context(restarted)

    async def forbid_gateway_transfer(*_args, **_kwargs):
        pytest.fail("本地 publication 冷恢复不得要求 Gateway 补写或重取 source")

    monkeypatch.setattr(recovered.forks._transport, "fork_transfer", forbid_gateway_transfer)
    async with main.lifespan(main.app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": container.workspace_id,
                 "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84"},
    ) as client:
        cold_catalog = recovered.sessions.path_resolver.catalog_store
        published = cold_catalog.get_creation_record(key)
        assert published.state == "published"
        for field in ("session_id", "main_thread_id", "created_at", "storage_relative_locator",
                      "preimage_hash", "request_intent_json", "session_metadata_json", "graph_binding"):
            assert getattr(published, field) == getattr(frozen, field)
        cold_root = recovered.sessions.path_resolver.resolve_session_node(published.session_id)
        with closing(SessionControlStore(cold_root / "session-control.sqlite", read_only=True)) as control:
            committed, = read_copy_attachment_settlements(control)
            assert committed.state == "committed"
            for field in ("copy_operation_id", "preimage_hash", "publication_preimage_hash", "target_session_id",
                          "target_thread_id", "claim_ids", "claim_manifest_sha256", "captured_generation",
                          "lease_id", "lease_fencing_token"):
                assert getattr(committed, field) == getattr(reserved, field)
            assert control.get_lease(committed.lease_id).state == "completed"
        claims_before = [tuple(row) for row in restarted.attachment_blob_store._catalog._connected().execute(
            "SELECT claim_id,state FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
        )]
        assert claims_before == [(claim_id, "committed") for claim_id in reserved.claim_ids]
        replies = []
        for attempt in range(2):
            request_id = f"copy-publication-{window}-retry-{attempt}"
            response = await client.post(
                f"/api/v1/sessions/{source.session_id}/fork-context", json=fork_request.model_dump(mode="json"),
                headers={"X-Request-ID": request_id},
            )
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
            reply = response.json()["data"]
            assert (reply["session"]["session_id"], reply["thread_id"]) == (frozen.session_id, frozen.main_thread_id)
            assert len(reply["resources"]) == 1
            content = await client.get(
                f"/api/v1/sessions/{frozen.session_id}/attachments/content",
                params={"file_id": reply["resources"][0]["target_uri"], "variant": "original"},
                headers={"X-Request-ID": request_id + "-attachment"},
            )
            assert content.status_code == 200 and content.content == "冷恢复附件正文".encode()
            replies.append(reply)
        assert replies[0] == replies[1]
        assert cold_catalog.get_creation_record(key) == published
        assert [tuple(row) for row in restarted.attachment_blob_store._catalog._connected().execute(
            "SELECT claim_id,state FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
        )] == claims_before
        assert (cold_root / "threads" / frozen.main_thread_id / "rollout/rollout.jsonl").read_bytes() == frozen_bytes
        assert (await recovered.sessions.list(limit=100)).total == 2
        artifacts = TestRunContext.from_test_file(Path(__file__)).prepare().artifacts_dir
        (artifacts / f"{window}-{frozen.session_id}.json").write_text(json.dumps({
            "window": window, "stages": stages, "session_id": frozen.session_id, "thread_id": frozen.main_thread_id,
            "publication_preimage_hash": frozen.preimage_hash, "copy_preimage_hash": reserved.preimage_hash,
            "settlement_before": reserved.state, "settlement_after": committed.state,
            "lease_before": old_lease.state, "lease_after": "completed", "claim_ids": committed.claim_ids,
            "history_sha256": hashlib.sha256(frozen_bytes).hexdigest(), "idempotent_replies": len(replies),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
