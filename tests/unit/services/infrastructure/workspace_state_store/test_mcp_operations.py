"""旧 v14 库升级与操作意图的事务、唯一性和终态 fence。"""

from __future__ import annotations

from contextlib import closing

import pytest

from app.core.sqlite_state import SQLiteStateDatabase
from app.services.infrastructure.mcp.operation_store import McpConnectionOperationStore
from app.services.infrastructure.workspace_state_store import (
    _WORKSPACE_MIGRATIONS,
    WorkspaceStateStore,
)


@pytest.fixture
def operation_store(workspace_root):
    database = SQLiteStateDatabase(
        path=workspace_root / ".boxteam/state/workspace.sqlite", schema_version=14, migrations=_WORKSPACE_MIGRATIONS[:14],
    )
    with closing(database.connection()) as connection:
        connection.execute("INSERT INTO config_source_layers(config_key, config_version, payload_json, layer_revision, source_generation, presence, uri, layer_digest, updated_at) VALUES ('original', 1, '{}', 1, 1, 'present', NULL, 'old-digest', '2026-10-27')")
    database.close()
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        assert store.diagnostics().schema_version == 20
        with closing(store.connection()) as connection:
            assert connection.execute("SELECT layer_digest FROM config_source_layers WHERE config_key = 'original'").fetchone()[0] == "old-digest"
        yield McpConnectionOperationStore(store), store
    finally:
        store.close()


def test_active_connection_aliases_and_terminal_replay_are_durable(operation_store):
    operations, store = operation_store
    request = {"operation_id": "first", "connection_id": "workspace:server", "server_id": "server", "action": "connect", "configuration_ref": "config-v1", "deadline": 9999999999}
    original, created = operations.begin(**request)
    assert created
    joined, created = operations.begin(**{**request, "operation_id": "second"})
    assert not created and joined == original
    with pytest.raises(RuntimeError, match="相反操作"):
        operations.begin(**{**request, "operation_id": "disconnect", "action": "disconnect"})
    with pytest.raises(ValueError, match="不同连接意图"):
        operations.begin(**{**request, "connection_id": "another-workspace:server"})
    complete = operations.advance("first", status="succeeded", stage="complete")
    # 晚回调不能改写终态，即使原配置已改变，两个 intent 都返回同一原事实。
    assert operations.advance("first", status="waiting_user", stage="authorization") == complete
    cold = McpConnectionOperationStore(store)
    assert cold.get("second") == complete
    replay, created = cold.begin(**{**request, "operation_id": "second", "configuration_ref": "config-v2"})
    assert replay == complete and not created
    next_operation, created = cold.begin(**{**request, "operation_id": "third", "action": "disconnect"})
    assert created and next_operation.operation_id == "third" and len(cold.active()) == 1
