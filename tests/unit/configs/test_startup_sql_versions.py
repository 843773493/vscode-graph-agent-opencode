"""正常启动预检识别原 owner 的 SQL 账本，实际升级仍由 store 启动执行。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.sqlite_state import Migration, SQLiteStateDatabase
from app.gateway.control.gateway_state import _GATEWAY_MIGRATIONS, GatewayStateStore
from app.services.infrastructure.workspace_state_store import (
    _WORKSPACE_MIGRATIONS,
    WorkspaceStateStore,
)
from configs.cli import main
from configs.maintenance.workspace_identity_commit import (
    commit_workspace_identity_migration,
)
from tests.unit.configs.conftest import LegacyGateway


@dataclass(frozen=True)
class SQLSource:
    path: Path
    workspace: Path
    domain: str
    migrations: tuple[Migration, ...]


@pytest.fixture(params=["gateway", "workspace"])
def sql_source(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SQLSource:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--preview", "--project-root", str(Path.cwd()), "--workspace", str(workspace)])
    if request.param == "gateway":
        return SQLSource(home / "state/gateway/gateway.sqlite", workspace, request.param, _GATEWAY_MIGRATIONS)
    return SQLSource(workspace / ".boxteam/state/workspace.sqlite", workspace, request.param, _WORKSPACE_MIGRATIONS)


@pytest.mark.parametrize("preview", [True, False])
def test_sql_upgrade_is_read_only_until_original_owner_starts(
    sql_source: SQLSource, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], preview: bool,
) -> None:
    case = sql_source
    current = len(case.migrations) - 1
    with SQLiteStateDatabase(path=case.path, schema_version=current, migrations=case.migrations[:current]):
        pass
    before = case.path.read_bytes()
    if not preview:
        monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd()), "--workspace", str(case.workspace)])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["upgrades"] == {"sqlite": {
        "state": "migration_required" if preview else "pending_owner_startup",
        "databases": [{"path": str(case.path), "current_schema_version": current, "target_schema_version": len(case.migrations)}],
    }}
    assert case.path.read_bytes() == before
    assert not list(case.path.parent.glob("*.bak"))
    owner = (GatewayStateStore(path=case.path) if case.domain == "gateway"
             else WorkspaceStateStore(workspace_root=case.workspace))
    with owner:
        assert owner.diagnostics().schema_version == len(case.migrations)
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {}


def test_current_sql_version_does_not_scan_session_databases(
    sql_source: SQLSource, capsys: pytest.CaptureFixture[str],
) -> None:
    case = sql_source
    with SQLiteStateDatabase(path=case.path, schema_version=len(case.migrations), migrations=case.migrations):
        pass
    session_database = case.workspace / ".boxteam/sessions/2026/11/14/session/logs/traces/trace.sqlite"
    session_database.parent.mkdir(parents=True)
    session_database.write_bytes(b"unrelated session database")
    before = case.path.read_bytes()
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["upgrade_required"] is False
    assert report["upgrades"] == {}
    assert case.path.read_bytes() == before
    assert session_database.read_bytes() == b"unrelated session database"
    assert not list(case.path.parent.glob("*.bak"))


@pytest.mark.parametrize("fault,reason", [("future", "版本未知"), ("gap", "缺号"), ("missing", "来源未知")])
@pytest.mark.parametrize("preview", [True, False])
def test_unknown_or_incomplete_sql_ledger_preserves_source(
    sql_source: SQLSource, monkeypatch: pytest.MonkeyPatch, fault: str, reason: str, preview: bool,
) -> None:
    case = sql_source
    with SQLiteStateDatabase(path=case.path, schema_version=len(case.migrations), migrations=case.migrations):
        pass
    with closing(sqlite3.connect(case.path)) as connection:
        if fault == "future":
            connection.execute("INSERT INTO schema_migrations VALUES (?, 'future')", (len(case.migrations) + 1,))
        elif fault == "gap":
            connection.execute("DELETE FROM schema_migrations WHERE version=1")
        else:
            connection.execute("DROP TABLE schema_migrations")
        connection.commit()
    before = case.path.read_bytes()
    if not preview:
        monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd()), "--workspace", str(case.workspace)])
    with pytest.raises(ValueError, match=reason):
        main()
    assert case.path.read_bytes() == before
    assert not list(case.path.parent.glob("*.bak"))


def test_sql_preview_reads_registered_local_roots(legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    case = legacy_gateway
    commit_workspace_identity_migration(gateway_root=case.root, project_root=Path.cwd())
    with GatewayStateStore(path=case.database) as gateway:
        registry = gateway.load_workspace_registry()
        assert registry is not None
        workspace = Path(next(target["root_path"] for target in registry["targets"] if target["connection_kind"] == "local"))
    path = workspace / ".boxteam/state/workspace.sqlite"
    current = len(_WORKSPACE_MIGRATIONS) - 1
    with SQLiteStateDatabase(path=path, schema_version=current, migrations=_WORKSPACE_MIGRATIONS[:current]):
        pass
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--preview", "--project-root", str(Path.cwd())])
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {"sqlite": {
        "state": "migration_required",
        "databases": [{"path": str(path), "current_schema_version": current, "target_schema_version": len(_WORKSPACE_MIGRATIONS)}],
    }}
