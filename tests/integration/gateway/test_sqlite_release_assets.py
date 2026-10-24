"""通过发行包真实资源收集入口验证 SQL 的离开源码树运行能力。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def staged_application(tmp_path: Path):
    application = tmp_path / "发行应用"
    result = subprocess.run([
        "bun", "--eval",
        ('import { copyApplicationSources } from "./packaging/runtime/application-sources.mjs"; '
         'copyApplicationSources(process.cwd(), process.argv[1]);'),
        str(application),
    ], capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "copy-result.json").write_text(json.dumps({
        "argv": result.args, "exit_code": result.returncode,
        "stdout": result.stdout, "stderr": result.stderr,
    }, ensure_ascii=False, indent=2))
    assert result.returncode == 0, result.stderr
    return application


def test_release_copy_loads_sql_initializes_reopens_and_missing_asset_fails(
    staged_application: Path, tmp_path: Path,
):
    script = '''
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import app.core.sqlite_state as core
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_control_store import SessionControlStore
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation.store import federation_control_database
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from app.services.infrastructure.workspace_files.store import WorkspaceFileStore
from app.services.infrastructure.config.source_owner import WorkspaceSourceOwner
from app.services.infrastructure.attachment_blob_catalog.catalog import AttachmentBlobCatalog
from app.services.infrastructure.rollout_context.storage.schema import initialize_rollout_schema
from app.services.infrastructure.rollout_context.activation.schema import (
    create_resource_activation_schema, require_resource_activation_schema,
)
assert Path(core.__file__).is_relative_to(Path.cwd())
for _ in range(2):
    with GatewayStateStore(path=Path("state/gateway.sqlite")) as store:
        store.set_config(config_key="release-marker", config_version=3, payload={"value": "中文"})
        assert store.get_config("release-marker").payload == {"value": "中文"}
        assert store.diagnostics().schema_version == 17
    with federation_control_database(gateway_root=Path("state")) as database:
        connection = database.connection()
        assert connection.execute("SELECT COUNT(*) FROM federation_replay_registry").fetchone()[0] == 0
        connection.close()
    with closing(SessionCatalogStore(Path("state/catalog.sqlite"), Path("state/sessions"))) as catalog:
        assert catalog.connection.execute("SELECT count(*) FROM navigation_mutation_records").fetchone()[0] == 0
        assert catalog.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with closing(SessionControlStore(Path("state/session-control.sqlite"))) as control:
        assert control.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert control.connection.execute("SELECT revision FROM collaboration_ledger").fetchone()[0] == 0
    with closing(WorkspaceStateStore(workspace_root=Path("state/workspace"))) as workspace:
        with closing(workspace.connection()) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with closing(WorkspaceFileStore(Path("state/workspace"), "workspace-release")) as files:
        assert files.path.is_file()
    with closing(WorkspaceSourceOwner(path=Path("state/source-owner.sqlite"))) as source:
        assert source.high_water_mark() == 0
    with closing(AttachmentBlobCatalog(Path("state/attachments/catalog.sqlite"))) as attachments:
        assert attachments.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    rollout_path = Path("state/rollout.sqlite")
    fresh_rollout = not rollout_path.exists()
    with closing(sqlite3.connect(rollout_path)) as rollout:
        if fresh_rollout:
            initialize_rollout_schema(rollout, initialize_activation=create_resource_activation_schema)
            rollout.commit()
        require_resource_activation_schema(rollout)
        assert rollout.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert rollout.execute("SELECT count(*) FROM context_assemblies").fetchone()[0] == 0
print(json.dumps({"loaded": core.__file__, "schema": 17}))
'''
    environment = {**os.environ, "PYTHONPATH": str(staged_application)}

    def run(name):
        result = subprocess.run([sys.executable, "-c", script], cwd=staged_application,
                                env=environment, capture_output=True, text=True, timeout=30, check=False)
        (tmp_path / f"{name}.json").write_text(json.dumps({
            "argv": result.args, "cwd": str(staged_application), "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }, ensure_ascii=False, indent=2))
        return result

    passed = run("staged-initialize-reopen")
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout)["schema"] == 17
    asset = staged_application / "app/gateway/control/gateway_state/sql/001.sql"
    asset.unlink()
    missing = run("staged-missing-asset")
    assert missing.returncode != 0 and "FileNotFoundError" in missing.stderr
    assert "001.sql" in missing.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux standalone Python 的原生探针")
def test_builtin_sqlite_vfs_probe_uses_current_connection_api(
    staged_application: Path, tmp_path: Path,
):
    probe_root = staged_application / "app/core"
    compiled = subprocess.run([
        "cc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
        "-o", str(probe_root / "sqlite_vfs_probe.so"),
        str(probe_root / "sqlite_vfs_probe.c"),
    ], capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "compile-probe.json").write_text(json.dumps({
        "argv": compiled.args, "exit_code": compiled.returncode,
        "stdout": compiled.stdout, "stderr": compiled.stderr,
    }, ensure_ascii=False, indent=2))
    assert compiled.returncode == 0, compiled.stderr
    script = '''
import _sqlite3
import sqlite3
from contextlib import closing
from pathlib import Path
from app.core.sqlite_state import sqlite_vfs_max_pathname, validate_sqlite_path_budget

# 用同一解释器的原生符号结果核验 extension API；分支不读取扩展文件。
expected = sqlite_vfs_max_pathname()
_sqlite3.__spec__.origin = "built-in"
_sqlite3.__file__ = None
assert sqlite_vfs_max_pathname() == expected
database = Path("state/native.sqlite")
validate_sqlite_path_budget(database)
database.parent.mkdir(parents=True)
with closing(sqlite3.connect(database)) as connection:
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("CREATE TABLE probe (value TEXT)")
    connection.execute("INSERT INTO probe VALUES ('发行版 Ω')")
    connection.commit()
with closing(sqlite3.connect(database)) as connection:
    assert connection.execute("SELECT value FROM probe").fetchone()[0] == "发行版 Ω"
Path("app/core/sqlite_vfs_probe.so").unlink()
try:
    sqlite_vfs_max_pathname()
except sqlite3.OperationalError:
    pass
else:
    raise AssertionError("探针缺失必须报错，不能查询另一份 SQLite")
print(expected)
'''
    result = subprocess.run([
        sys.executable, "-c", script,
    ], cwd=staged_application,
        env={**os.environ, "PYTHONPATH": str(staged_application)},
        capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "builtin-sqlite-probe.json").write_text(json.dumps({
        "argv": result.args, "cwd": str(staged_application), "exit_code": result.returncode,
        "stdout": result.stdout, "stderr": result.stderr,
    }, ensure_ascii=False, indent=2))
    assert result.returncode == 0, result.stderr


def test_release_copy_loads_managed_directory_schema(staged_application: Path, tmp_path: Path):
    script = """
import sqlite3
from pathlib import Path
from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    initialize_directory_schema, require_directory_schema,
)
assert Path(__import__("app").__file__).is_relative_to(Path.cwd())
with sqlite3.connect(":memory:") as connection:
    initialize_directory_schema(connection)
    require_directory_schema(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
"""
    environment = {**os.environ, "PYTHONPATH": str(staged_application)}
    for name, available in (("managed-directory-present", True), ("managed-directory-missing", False)):
        if not available:
            (staged_application / "app/services/infrastructure/attachment_blob_catalog/sql/managed_directory_schema.sql").unlink()
        result = subprocess.run([sys.executable, "-c", script], cwd=staged_application,
                                env=environment, capture_output=True, text=True, timeout=30, check=False)
        (tmp_path / f"{name}.json").write_text(json.dumps({
            "argv": result.args, "cwd": str(staged_application), "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }, ensure_ascii=False, indent=2))
        if available:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0 and "FileNotFoundError" in result.stderr
            assert "managed_directory_schema.sql" in result.stderr
