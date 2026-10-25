"""文件导航库初始化失败必须关闭原连接，并保留可重试的原库。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.sqlite_state import sqlite_file_uri
from app.services.infrastructure.workspace_files import store as store_module
from app.services.infrastructure.workspace_files.store import WorkspaceFileStore


@pytest.fixture
def initialization_case(tmp_path, monkeypatch):
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    workspace_id = str(uuid4())
    with closing(WorkspaceFileStore(workspace, workspace_id)) as store:
        path = store.path
    baseline = path.read_bytes()
    connect = sqlite3.connect
    connections = []

    class ObservedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def observe_connection(database, **kwargs):
        if database == sqlite_file_uri(path):
            kwargs["factory"] = ObservedConnection
        connection = connect(database, **kwargs)
        if isinstance(connection, ObservedConnection):
            connections.append(connection)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", observe_connection)
    try:
        yield SimpleNamespace(workspace=workspace, workspace_id=workspace_id, path=path,
                              baseline=baseline, connect=connect, connections=connections)
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("failure", ["version", "missing_table", "owner", "invalid_database", "sql_asset"])
def test_initialization_failure_closes_connection_without_gc_and_allows_repair(
    initialization_case, monkeypatch, caplog, failure,
):
    case = initialization_case
    private_value = str(case.workspace / "private-owner-value")
    if failure == "invalid_database":
        case.path.write_bytes(b"invalid sqlite database")
    elif failure == "sql_asset":
        case.path.unlink()
    else:
        with closing(case.connect(case.path, isolation_level=None)) as connection:
            if failure == "version":
                connection.execute("PRAGMA user_version=99")
            elif failure == "missing_table":
                connection.execute("DROP TABLE events")
            else:
                connection.execute("UPDATE owner SET workspace_id=?", (private_value,))
    before = case.path.read_bytes() if case.path.exists() else None
    expected_type = sqlite3.DatabaseError if failure == "invalid_database" else FileNotFoundError if failure == "sql_asset" else RuntimeError
    load_sql = store_module.load_sql
    missing_asset = case.workspace / "private-sql-assets/missing.sql"

    def load_asset(package, resource):
        if resource == "sql/events.sql":
            return missing_asset.read_text(encoding="utf-8")
        return load_sql(package, resource)

    with monkeypatch.context() as patch:
        if failure == "sql_asset":
            patch.setattr(store_module, "load_sql", load_asset)
        with pytest.raises(expected_type) as caught:
            WorkspaceFileStore(case.workspace, case.workspace_id)
        # 保留异常 traceback 与真实 Connection 引用，排除 GC 顺带回收的假通过。
        assert len(case.connections) == 1
        assert case.connections[0].closed
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            case.connections[0].execute("SELECT 1")
        for private in (str(case.path), str(missing_asset), private_value):
            assert private not in str(caught.value)
        assert str(case.path) in caplog.text or str(missing_asset) in caplog.text
        if failure == "sql_asset":
            assert caught.value.__cause__.filename == str(missing_asset)
            assert caught.value.errno == caught.value.__cause__.errno
            assert getattr(caught.value, "winerror", None) == getattr(caught.value.__cause__, "winerror", None)
            assert caught.value.filename is None
            with closing(case.connect(case.path)) as connection:
                assert connection.execute("PRAGMA user_version").fetchone() == (0,)
                assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        if failure == "invalid_database":
            assert type(caught.value.__cause__) is expected_type
            assert caught.value.sqlite_errorcode == caught.value.__cause__.sqlite_errorcode
            assert caught.value.sqlite_errorname == caught.value.__cause__.sqlite_errorname
        if before is not None:
            assert case.path.read_bytes() == before
    # 修复损坏输入后原owner立即重开，不重建另一套数据库或等待GC。
    case.path.write_bytes(case.baseline)
    with closing(WorkspaceFileStore(case.workspace, case.workspace_id)) as recovered:
        assert recovered.pending() == ()
        assert recovered.events(0) == []


def test_successful_initialization_retains_connection_until_owner_close(initialization_case):
    case = initialization_case
    store = WorkspaceFileStore(case.workspace, case.workspace_id)
    assert len(case.connections) == 1 and not case.connections[0].closed
    assert store.pending() == ()
    store.close()
    assert case.connections[0].closed
