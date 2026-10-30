"""私有快照校验失败不公开位置，由原 fork owner 中止并释放作用域。"""

from __future__ import annotations

import errno
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.container import AppContainer
from app.schemas.internal_v2.session import SessionDTO
from app.services.infrastructure.rollout_context.checkpoint import fork_source
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


async def test_snapshot_scope_native_failure_aborts_only_original_target(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    )
    key = context.forks._creation_key(request.idempotency_key)
    catalog = context.sessions.path_resolver.catalog_store
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    visible = {item.session_id for item in (await context.sessions.list()).items}
    observed: list[tuple[Path, PermissionError]] = []
    original_observer = fork_source.observe_phase
    original_resolve = Path.resolve

    async def fail_snapshot_resolution(
        operation_id: str, phase: str, *, details: Mapping[str, object] | None = None,
    ) -> None:
        await original_observer(operation_id, phase, details=details)
        if operation_id != key or phase != "copy.source.partial":
            return
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        assert capture is not None and capture.state == "preparing"
        captured = capture_directory(context.saver._storage.storage.sessions_dir, capture) / source.session_id
        native = PermissionError(errno.EACCES, f"私有快照解析失败 {captured}", str(captured))

        def reject_once(path: Path, strict: bool = False) -> Path:
            if path == captured and not observed:
                observed.append((captured, native))
                raise native
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", reject_once)

    monkeypatch.setattr(fork_source, "observe_phase", fail_snapshot_resolution)
    with pytest.raises(PermissionError) as caught:
        await context.forks.fork(request)
    (captured, native), = observed
    error = caught.value
    assert error.__cause__ is native and error.errno == errno.EACCES and error.filename is None
    assert str(captured) not in str(error)
    assert "stage=fork_source_snapshot" in str(error)
    assert source.session_id in str(error) and "thread_id=" in str(error)
    assert any(row.exc_info and row.exc_info[1] is native for row in caplog.records)
    creation = catalog.get_creation_record(key)
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    assert creation.state == "aborted" and capture is not None and capture.state == "aborted"
    assert capture.marker_sha256 is None
    assert context.saver._storage.storage.fork_paths.source_snapshot(source.session_id) is None
    assert canonical.read_bytes() == original
    assert {item.session_id for item in (await context.sessions.list()).items} == visible
    with pytest.raises(KeyError):
        catalog.get_node(creation.session_id)
    with pytest.raises(RuntimeError, match="已中止"):
        await context.forks.fork(request)
    assert catalog.get_creation_record(key) == creation

    # 原操作已明确中止；解除故障后的新意图仍经原产品入口发布，不能复活旧目标。
    next_request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    )
    result = await context.forks.fork(next_request)
    assert result.session.session_id != creation.session_id
    assert await context.forks.fork(next_request) == result
    assert canonical.read_bytes() == original
    copied = _main_thread_node(context, result.session.session_id) / "rollout/rollout.jsonl"
    assert copied.read_bytes() == original
