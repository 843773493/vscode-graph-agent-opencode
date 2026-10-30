"""child 已发布后的确认失败必须保留原事实，并由同一操作补齐。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.container import AppContainer
from app.core import thread_creation
from app.core.session_control_store.copy_attachment_settlement import (
    read_copy_attachment_settlements,
)
from app.schemas.internal_v2.session import SessionDTO
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
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


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("fault", ["before_callback", "after_callback", "phase"])
async def test_child_publication_failure_preserves_original_fork_and_retries(
    production_fork_container: AppContainer,
    fork_execution_source: SessionDTO,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    request = await _fork_request(context, source.session_id, creation_kind="new_child_thread")
    key = context.forks._creation_key(request.idempotency_key)
    catalog = context.sessions.path_resolver.catalog_store
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    source_bytes = canonical.read_bytes()
    attachments = production_fork_container.attachment_blob_store
    original_mark = attachments.mark_copy_published
    failure = RuntimeError(f"child publication {fault} 故障")
    callbacks = []

    def fail_confirmation(capture, *, publication_preimage_hash: str) -> None:
        callbacks.append(capture.copy_operation_id)
        assert capture.creation_key == key
        if fault == "before_callback":
            raise failure
        original_mark(capture, publication_preimage_hash=publication_preimage_hash)
        if fault == "after_callback":
            raise failure

    async def fail_phase(operation_id: str, phase: str) -> None:
        if operation_id == key and phase == "thread.creation.published" and fault == "phase":
            raise failure

    original_observe = thread_creation.observe_phase

    async def observe(
        operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        await fail_phase(operation_id, phase)
        await original_observe(operation_id, phase, details=details)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(attachments, "mark_copy_published", fail_confirmation)
            patch.setattr(thread_creation, "observe_phase", observe)
            with pytest.raises(RuntimeError) as caught:
                await context.forks.fork(request)
            assert caught.value is failure
        assert callbacks == [key]
        capture = catalog.get_fork_source_capture(creation_key=key)
        assert capture is not None and capture.state == "materialized"
        assert capture.target_session_id == source.session_id
        claims = attachments._catalog.fork_attachment_claim_ids(capture.copy_operation_id)
        assert claims
        with context.forks._threads.for_owner_session(source.session_id) as service:
            control = service.control_store
            record = control.get_thread_creation_record(key)
            assert record.state == "published" and record.abort_reason is None
            assert record.child_thread_id == capture.target_thread_id
            node = context.sessions.path_resolver.resolve_session_node(source.session_id) / record.final_relative_locator
            target_bytes = (node / "rollout/rollout.jsonl").read_bytes()
            assert target_bytes == source_bytes
            settlements = [item for item in read_copy_attachment_settlements(control) if item.copy_operation_id == key]
            assert len(settlements) == 1
            assert settlements[0].state == ("preparing" if fault == "before_callback" else "published_pending_attachment_commit")
            assert control.connection.execute("SELECT COUNT(*) FROM thread_catalog").fetchone()[0] == 2
            assert control.connection.execute("SELECT COUNT(*) FROM thread_execution_intents").fetchone()[0] == 0

            async def no_materialization(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("已发布 child 重试不能重新物化")

            with monkeypatch.context() as patch:
                patch.setattr(context.forks, "_materialize", no_materialization)
                result = await context.forks.fork(request)
                assert await context.forks.fork(request) == result
            assert result.session.session_id == source.session_id
            assert result.thread_id == record.child_thread_id
            assert control.get_thread_creation_record(key) == record
            assert control.connection.execute("SELECT COUNT(*) FROM thread_catalog").fetchone()[0] == 2
            assert control.connection.execute("SELECT COUNT(*) FROM thread_execution_intents").fetchone()[0] == 0
            assert [item.state for item in read_copy_attachment_settlements(control) if item.copy_operation_id == key] == ["committed"]
        assert attachments._catalog.fork_attachment_claim_ids(capture.copy_operation_id) == claims
        assert catalog.get_fork_source_capture(creation_key=key) == capture
        assert canonical.read_bytes() == source_bytes
        assert (node / "rollout/rollout.jsonl").read_bytes() == target_bytes
    finally:
        # 断言失败也经产品入口结清原操作及固定 watchdog，不重开期限。
        await context.forks.fork(request)
