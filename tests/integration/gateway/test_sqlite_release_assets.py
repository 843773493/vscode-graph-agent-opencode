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
from pathlib import Path
import app.core.sqlite_state as core
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation.store import federation_control_database
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
