"""已发布 fork 的来源释放失败不受诊断次错干扰。"""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.resource_uri import thread_resource_uri
from app.core.session_catalog_store import SessionCatalogStore
from app.core.trace_middleware import (
    get_current_gateway_id,
    get_current_request_id,
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_fork_transfer import (
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
)
from app.workspace_lifecycle import _retry_pending_fork_exports_periodically
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    SourceForkOwnerTransport,
    _fork_request,
    _main_thread_id,
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


@pytest.mark.parametrize("cancel_retry", [False, True], ids=["connection", "cancelled"])
async def test_published_export_preserves_pending_cancellation_and_periodic_retry(
    production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    fork_execution_source: SessionDTO,
    fork_diagnostic_sink: tuple[Path, list[BaseException]] | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    inprocess_model_replay,
    cancel_retry: bool,
) -> None:
    source = _production_fork_context(production_fork_container)
    session = fork_execution_source
    target_container, transport = remote_fork_target
    target = _production_fork_context(target_container)
    source_gateway = "gateway_4aae160f813ed87bcf65a8bb8bfd3729"
    target_gateway = "gateway_2d2ab24c1f08e28f5f0217140c8be744"
    request = (await _fork_request(
        source, session.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    )).model_copy(update={
        "source_uri": thread_resource_uri(session.workspace_id, session.session_id,
            _main_thread_id(source, session.session_id), gateway_authority=source_gateway),
        "target_container_uri": workspace_session_inventory_uri(
            target_container.workspace_id, gateway_authority=target_gateway),
    })
    key = target.forks._creation_key(request.idempotency_key)
    first_error = ConnectionError(13, "来源释放不可用", "/private/export/snapshot.sqlite")
    retry_error = (asyncio.CancelledError("取消来源释放: /private/export/snapshot.sqlite") if cancel_retry
                   else ConnectionError(5, "来源释放不可用", "/private/export/snapshot.sqlite"))
    worker_error = ConnectionError(16, "来源释放不可用", "/private/export/snapshot.sqlite")
    original_transfer = transport.fork_transfer
    release_attempts = 0
    contexts: list[tuple[str | None, str | None]] = []
    final_attempt = asyncio.Event()
    recovered = asyncio.Event()
    completed = asyncio.Event()
    worker: asyncio.Task[None] | None = None
    _, controller = inprocess_model_replay
    model_calls = controller.transport.call_count

    async def release_with_failure(payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        nonlocal release_attempts
        if payload.action == "release":
            contexts.append((get_current_gateway_id(), get_current_request_id()))
            release_attempts += 1
            if release_attempts <= 3:
                raise (first_error, retry_error, worker_error)[release_attempts - 1]
            final_attempt.set()
            await recovered.wait()
            result = await original_transfer(payload)
            completed.set()
            return result
        return await original_transfer(payload)

    gateway_token = set_current_gateway_id(target_gateway)
    request_token = set_current_request_id("export-release-diagnostics")
    try:
        monkeypatch.setattr(transport, "fork_transfer", release_with_failure)
        result = await target.forks.fork(request)
        catalog = target.sessions.path_resolver.catalog_store
        published = catalog.get_creation_record(key)
        assert published.state == "published"
        assert result.export_release is not None and result.export_release.state == "pending"
        assert "error=ConnectionError" in (result.export_release.error or "")
        assert "errno=13" in (result.export_release.error or "")
        assert "private" not in result.model_dump_json()
        source_capture = source.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
        assert source_capture is not None and source_capture.state == "source_captured"
        snapshot = capture_directory(source.saver._storage.storage.sessions_dir, source_capture)
        assert snapshot.is_dir()

        # 恢复必须沿用 creation 冻结的上下文，且返回后恢复调用者上下文。
        caller_request = set_current_request_id("independent-export-retry")
        caller_gateway = set_current_gateway_id(None)
        try:
            if cancel_retry:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await target.forks.retry_pending_remote_exports()
                assert caught.value is retry_error
            else:
                await target.forks.retry_pending_remote_exports()
            assert get_current_gateway_id() is None
            assert get_current_request_id() == "independent-export-retry"
        finally:
            reset_current_gateway_id(caller_gateway)
            reset_current_request_id(caller_request)
        state, detail = catalog.get_fork_export_release(key)
        assert state == "pending" and detail is not None
        assert f"error={type(retry_error).__name__}" in detail and "private" not in detail
        assert catalog.get_creation_record(key) == published and snapshot.is_dir()

        # 运行生产周期协程；首轮失败不能让 worker 退出，下一轮仍释放同一 capture。
        worker = asyncio.create_task(_retry_pending_fork_exports_periodically(target_container))
        await asyncio.wait_for(final_attempt.wait(), timeout=5)
        assert not worker.done()
        assert catalog.get_fork_export_release(key)[0] == "pending"
        assert catalog.get_creation_record(key) == published and snapshot.is_dir()
        with closing(SessionCatalogStore(catalog.database_path, catalog.sessions_root)) as cold:
            assert cold.get_fork_export_release(key) == catalog.get_fork_export_release(key)
            with cold.read_transaction() as connection:
                assert "private" not in "\n".join(connection.iterdump())
        errors = [first_error, retry_error, worker_error]
        if fork_diagnostic_sink is None:
            assert all(any(record.exc_info and record.exc_info[1] is error for record in caplog.records) for error in errors)
        else:
            blocked, logged = fork_diagnostic_sink
            assert logged == errors
            assert "diagnostic_sink_error=NotADirectoryError" in (result.export_release.error or "")
            assert "diagnostic_sink_error=NotADirectoryError" in detail
            for error in errors:
                assert str(blocked) in "\n".join(error.__notes__)
        assert first_error.filename == worker_error.filename == "/private/export/snapshot.sqlite"
        if isinstance(retry_error, ConnectionError):
            assert retry_error.filename == "/private/export/snapshot.sqlite" and retry_error.errno == 5
        else:
            assert retry_error.args == ("取消来源释放: /private/export/snapshot.sqlite",)
        recovered.set()
        await asyncio.wait_for(completed.wait(), timeout=5)
        assert catalog.get_fork_export_release(key) == ("confirmed", None)
        assert catalog.get_creation_record(key) == published and not snapshot.exists()
        assert contexts == [(target_gateway, "export-release-diagnostics")] * 4
        replay = await target.forks.fork(request)
        assert replay.export_release is not None and replay.export_release.state == "confirmed"
        assert replay.model_copy(update={"export_release": result.export_release}) == result
        assert sum(call.action == "capture" for call in transport.calls) == 1
        assert sum(call.action == "release" for call in transport.calls) == 1
        assert controller.transport.call_count == model_calls
        assert await target_container.job_service.list() == []
    finally:
        recovered.set()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        monkeypatch.setattr(transport, "fork_transfer", original_transfer)
        # 反例失败也由产品 owner 结清原 key，不覆盖或重开原60秒期限。
        try:
            await target.forks.fork(request)
        finally:
            reset_current_request_id(request_token)
            reset_current_gateway_id(gateway_token)
