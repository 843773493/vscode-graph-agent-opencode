"""真实 child fork 物化失败的中止记录只保存逻辑 owner，原始错误供管理诊断。"""

from __future__ import annotations

import errno
import logging
from contextlib import closing

import pytest

from app.container import AppContainer
from app.core.session_control_store import SessionControlStore
from app.core.thread_creation import ThreadCreationAbortedError
from app.schemas.internal_v2.session import SessionDTO
from app.services.infrastructure.rollout_context.checkpoint import fork_owner
from app.services.infrastructure.rollout_context.fork.source_capture import (
    capture_directory,
)
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


@pytest.mark.parametrize("locator", ["/private/fork.sqlite", r"C:\private\fork.sqlite", r"\\server\private\fork.sqlite"])
@pytest.mark.parametrize("failure_kind", ["native", "runtime"])
async def test_child_fork_materialization_keeps_safe_durable_failure(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    locator: str, failure_kind: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    source_node = _main_thread_node(context, source.session_id)
    original_bytes = (source_node / "rollout/rollout.jsonl").read_bytes()
    request = await _fork_request(
        context, source.session_id, creation_kind="new_child_thread", mode="full_rollout_copy",
    )
    key = context.forks._creation_key(request.idempotency_key)
    control = context.forks._threads.for_owner_session(source.session_id).control_store
    catalog = context.sessions.path_resolver.catalog_store
    observe = fork_owner.observe_phase
    original_error = (PermissionError(errno.EACCES, f"原生物化拒绝 {locator}", locator)
                      if failure_kind == "native" else RuntimeError(f"物化失败 {locator}"))
    captured = []

    async def fail_materialization(operation_id: str, phase: str, **kwargs: object):
        await observe(operation_id, phase, **kwargs)
        if operation_id == key and phase == "copy.source.before_captured_cas":
            captured.append(catalog.get_fork_source_capture(copy_operation_id=key))
            raise original_error

    with monkeypatch.context() as failure, caplog.at_level(logging.ERROR, logger="app.core.thread_creation"):
        failure.setattr(fork_owner, "observe_phase", fail_materialization)
        with pytest.raises(type(original_error)) as caught:
            await context.forks.fork(request)
    assert len(captured) == 1 and captured[0] is not None
    record = control.get_thread_creation_record(key)
    assert record.state == "aborted" and record.abort_reason
    assert source.session_id in record.abort_reason and record.child_thread_id in record.abort_reason
    assert "stage=fork_materialize" in record.abort_reason and type(original_error).__name__ in record.abort_reason
    assert locator not in record.abort_reason and str(context.workspace) not in record.abort_reason
    if failure_kind == "native":
        assert caught.value.__cause__ is original_error
        assert caught.value.errno == errno.EACCES
        assert caught.value.filename is None and locator not in str(caught.value)
    else:
        # 保留具有领域含义的 RuntimeError 对象与类型，不改写恢复协议。
        assert caught.value is original_error
    assert any(item.exc_info and item.exc_info[1] is original_error for item in caplog.records)
    assert any(locator in item.exc_text for item in caplog.records if item.exc_text)
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    assert capture is not None and capture.state == "aborted"
    assert locator not in (capture.abort_reason or "")
    assert not capture_directory(context.saver._storage.storage.sessions_dir, capture).exists()
    owner_node = context.sessions.path_resolver.resolve_session_node(source.session_id)
    assert not (owner_node / record.final_relative_locator).exists()
    assert not (owner_node / record.staging_locator).exists()
    assert not control.list_child_thread_rows() and not control.list_initial_execution_intents()
    with closing(SessionControlStore(control.database_path, read_only=True)) as cold:
        assert cold.get_thread_creation_record(key) == record
    with pytest.raises(ThreadCreationAbortedError) as replay:
        await context.forks.fork(request)
    assert replay.value.record == record and locator not in str(replay.value)
    assert (source_node / "rollout/rollout.jsonl").read_bytes() == original_bytes
