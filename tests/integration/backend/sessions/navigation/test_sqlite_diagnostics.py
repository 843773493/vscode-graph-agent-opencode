"""产品创建的 Session 在共享 SQLite 拒绝和诊断次错后仍能冷读。"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import portalocker
import pytest

from app.container import AppContainer, WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)


@pytest.mark.parametrize("fault", ["ownership", "schema"])
@pytest.mark.parametrize("broken_log", [False, True], ids=["diagnostics", "sink-failure"])
async def test_product_storage_rejection_keeps_original_session_and_cold_retry(
    startup_container: AppContainer, request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture, fault: str, broken_log: bool,
) -> None:
    container = startup_container
    session = await container.session_service.create(SessionCreateRequest(title="SQLite 失败前的原会话"))
    directory = container.session_service.path_resolver.resolve_session_node(session.session_id)
    body = {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    path = container.workspace_activity_service.store.path
    if fault == "schema":
        container.workspace_activity_service.store.close()
        container.session_catalog.close()
    healthy = path.read_bytes()
    if fault == "schema":
        with closing(sqlite3.connect(path)) as connection:
            version = int(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
            connection.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version + 1, "fault-input"))
            connection.commit()
    before = path.read_bytes()
    context = TestRunContext.from_test_file(Path(request.node.path))
    log_target = context.artifacts_dir / "private-log-target"
    log_original = "保留诊断冲突原件".encode()
    log_target.write_bytes(log_original)

    class FailedSink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if broken_log and "artifact=sqlite_runtime" in record.getMessage():
                (log_target / "error.log").write_text(record.getMessage(), encoding="utf-8")

    registry = WorkspaceContainerRegistry(
        mounted_workspaces=MountedWorkspaceRegistry([
            MountedWorkspace(container.workspace_id, container.workspace_root),
        ]),
        project_root=Path.cwd(),
    )
    request.addfinalizer(registry.close)
    handler = FailedSink()
    root = logging.getLogger()
    root.addHandler(handler)
    caplog.set_level(logging.ERROR)
    try:
        with pytest.raises(RuntimeError) as caught:
            registry.get(container.workspace_id)
    finally:
        root.removeHandler(handler)
        handler.close()
    public = str(caught.value)
    cause = caught.value.__cause__
    assert type(caught.value) is RuntimeError
    assert type(cause) is (portalocker.AlreadyLocked if fault == "ownership" else RuntimeError)
    assert f"stage={'ownership_acquire' if fault == 'ownership' else 'schema_version'}" in public
    assert "artifact=sqlite_runtime" in public
    assert str(container.workspace_root) not in public and str(log_target) not in public
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    diagnostic_path = path.with_name(f".{path.name}.lock") if fault == "ownership" else path
    assert str(diagnostic_path) in caplog.text
    if broken_log:
        assert "diagnostic_sink_error=NotADirectoryError" in public
        assert any(str(log_target) in note and "NotADirectoryError" in note
                   for note in getattr(cause, "__notes__", ()))
    else:
        assert "diagnostic_sink_error=" not in public
    assert path.read_bytes() == before
    assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == body
    assert log_target.read_bytes() == log_original
    if fault == "ownership":
        assert await container.session_service.get(session.session_id) == session
        container.workspace_activity_service.store.close()
        container.session_catalog.close()
    else:
        # 仅显式恢复故障前的原库；被测构造器不得补建表或重写损坏账本。
        assert not Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists()
        path.write_bytes(healthy)
    recovered = registry.get(container.workspace_id)
    request.addfinalizer(recovered.attachment_blob_store.close)
    request.addfinalizer(recovered.workspace_activity_service.store.close)
    assert await recovered.session_service.get(session.session_id) == session
    assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == body
    assert registry.get(container.workspace_id) is recovered
