"""未发布 fork 目标的控制库校验只读，不能在拒绝前修复原件。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.sqlite_state import sqlite_file_uri
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.session import SessionDTO
from app.services.infrastructure.rollout_context.checkpoint import fork_source
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


@pytest.mark.parametrize("fault", [
    "empty", "missing_table", "missing_graph_column", "delete_journal", "missing", "removed_before_open",
])
async def test_prepared_target_validation_preserves_control_and_original_fork(
    production_fork_container: AppContainer,
    fork_execution_source: SessionDTO,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    )
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    source_bytes = canonical.read_bytes()
    catalog = context.sessions.path_resolver.catalog_store
    visible = {item.session_id for item in (await context.sessions.list()).items}

    class OwnerProcessExit(BaseException):
        pass

    async def stop_before_materialization(*_args: object, **_kwargs: object) -> None:
        raise OwnerProcessExit("模拟创建目标后、物化前的进程退出")

    # 真实 fork 入口生成完整 preparing intent/目录；只在尚未物化处中断。
    with monkeypatch.context() as patch:
        patch.setattr(context.forks, "_materialize", stop_before_materialization)
        with pytest.raises(OwnerProcessExit):
            await context.forks.fork(request)
    key = context.forks._creation_key(request.idempotency_key)
    record = catalog.get_creation_record(key)
    assert record.state == "preparing"
    assert catalog.get_fork_source_capture(copy_operation_id=key) is None
    node = context.sessions.path_resolver.resolve_prepared_session_node(record)
    control = node / CONTROL_DATABASE_NAME
    # 产品只读校验可留下 WAL/SHM；故障装具由 SQLite 收束后冻结原件，
    # 同时以 DELETE 模式验证被测入口不会切换 journal 或补建旁文件。
    with closing(sqlite3.connect(control)) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    original = control.read_bytes()
    assert not control.with_name(control.name + "-wal").exists()
    assert not control.with_name(control.name + "-shm").exists()
    resolver = context.saver.forks.source.resolve_prepared_main_thread_owner
    original_store = fork_source.SessionControlStore
    try:
        if fault == "empty":
            control.write_bytes(b"")
        elif fault == "missing":
            control.unlink()
        else:
            with closing(sqlite3.connect(control)) as connection:
                if fault == "missing_table":
                    connection.execute("DROP TABLE thread_catalog")
                    connection.commit()
                elif fault == "missing_graph_column":
                    connection.execute("ALTER TABLE thread_catalog DROP COLUMN graph_schema_hash")
                    connection.commit()
        if fault == "removed_before_open":
            def remove_then_open(path: Path, *, read_only: bool = False):
                assert path == control
                control.unlink()
                return original_store(path, read_only=read_only)

            monkeypatch.setattr(fork_source, "SessionControlStore", remove_then_open)
        before = control.read_bytes() if control.exists() else None
        if fault == "delete_journal":
            assert resolver(record) == MutationIntentOwner(record.session_id, record.main_thread_id)
        else:
            with pytest.raises((RuntimeError, KeyError, sqlite3.Error)) as caught:
                resolver(record)
            assert str(node) not in str(caught.value)
            if fault == "missing":
                assert record.session_id in str(caught.value)
                assert record.main_thread_id in str(caught.value)
                assert CONTROL_DATABASE_NAME in str(caught.value)
                assert str(control) in "\n".join(caught.value.__notes__)
        if fault == "removed_before_open":
            # 检查完成后才丢失文件，真正的 SQLite open 也不能重建它。
            before = None
        assert (control.read_bytes() if control.exists() else None) == before
        assert not control.with_name(control.name + "-wal").exists()
        assert not control.with_name(control.name + "-shm").exists()
        if control.exists():
            with closing(sqlite3.connect(sqlite_file_uri(control) + "?mode=ro", uri=True)) as connection:
                if fault == "empty":
                    assert connection.execute("PRAGMA user_version").fetchone() == (0,)
                    assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == []
                else:
                    assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
                    if fault == "missing_table":
                        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='thread_catalog'").fetchall() == []
                    elif fault == "missing_graph_column":
                        assert "graph_schema_hash" not in {
                            row[1] for row in connection.execute("PRAGMA table_info(thread_catalog)")
                        }
        assert catalog.get_creation_record(key) == record
        assert catalog.get_fork_source_capture(copy_operation_id=key) is None
        assert {item.session_id for item in (await context.sessions.list()).items} == visible
        assert canonical.read_bytes() == source_bytes
    finally:
        # 恢复故障输入，原入口沿同一 intent 完成，不能让装具替产品发布/结算。
        monkeypatch.setattr(fork_source, "SessionControlStore", original_store)
        control.write_bytes(original)
        result = await context.forks.fork(request)
    assert result.session.session_id == record.session_id
    assert result.thread_id == record.main_thread_id
    assert catalog.get_creation_record(key).state == "published"
    assert await context.forks.fork(request) == result
    assert canonical.read_bytes() == source_bytes
    copied = _main_thread_node(context, result.session.session_id) / "rollout/rollout.jsonl"
    assert copied.read_bytes() == source_bytes
