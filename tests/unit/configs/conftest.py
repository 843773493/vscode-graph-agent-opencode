"""身份维护消费冻结的完整旧输入，不借新 runtime 伪造旧登记。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from configs.maintenance.workspace_identity_migration import preview_workspace_identity_migration


@dataclass
class LegacyGateway:
    directory: Path
    root: Path
    metadata: dict

    @property
    def database(self) -> Path:
        return self.root / "gateway.sqlite"

    @property
    def gateway_id(self) -> str:
        return self.metadata["gateway_identity"]["gateway_id"]

    def preview(self):
        return preview_workspace_identity_migration(gateway_root=self.root, project_root=Path.cwd())

    def dump(self) -> str:
        with closing(sqlite3.connect(self.database)) as connection:
            return "\n".join(connection.iterdump())


@pytest.fixture
def legacy_gateway(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LegacyGateway:
    directory = tmp_path / "legacy-gateway"
    root = directory / "gateway"
    root.mkdir(parents=True)
    source = Path.cwd() / "tests/fixtures/workspace_identity"
    metadata = json.loads((source / "registry_v13.json").read_text())
    escaped_root = json.dumps(str(directory))[1:-1].replace("'", "''")
    script = (source / "registry_v13.sql").read_text().replace("__TEST_ROOT__", escaped_root)
    with closing(sqlite3.connect(root / "gateway.sqlite")) as connection:
        connection.executescript(script)
        # 默认场景仅保留本地来源；远端用例消费同一完整冻结快照中的两条连接。
        if not getattr(request, "param", False):
            connection.execute("DELETE FROM gateway_workspace_registry WHERE workspace_id LIKE 'gw_remote_%'")
            raw = connection.execute("SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'").fetchone()[0]
            registry_metadata = json.loads(raw)
            registry_metadata["remote_gateway_connections"] = []
            connection.execute("UPDATE gateway_config SET payload_json=? WHERE config_key='workspace_registry_meta'", (json.dumps(registry_metadata),))
        connection.commit()
    (root / "identity.json").write_text(json.dumps(metadata["gateway_identity"]))
    navigation = root / "navigation/workspace-tree.json"
    navigation.parent.mkdir()
    navigation.write_text(json.dumps(metadata["navigation"]))
    for alias, value in metadata["workspaces"].items():
        native = directory / alias.replace("gw_local_", "workspace-")
        (native / ".boxteam").mkdir(parents=True)
        (native / ".boxteam/workspace-identity.json").write_text(json.dumps(value["identity"]))
    monkeypatch.setenv("BOXTEAM_HOME", str(directory / "home"))
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    return LegacyGateway(directory, root, metadata)
