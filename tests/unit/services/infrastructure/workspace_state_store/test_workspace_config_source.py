from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteStateDatabase
from app.services.infrastructure import workspace_state_store as store_module
from app.services.infrastructure.config.state import (
    SecretResolutionError,
    build_secret_binding_summary,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def _write_legacy_workspace_config_only_row(
    workspace_root, config_key: str, payload: dict
) -> None:
    """构造只有 legacy ``workspace_config`` 表行的旧库（无对应 layer 行）。

    旧库先只应用迁移 1..13（不含新增的一次性合并回调），再手工补一张 legacy
    ``workspace_config`` 并写入唯一一行。重新以当前 schema 打开时，合并回调必须
    把这一行还原为等价 present layer 行；这锁定 T-1 证据 3 的活兼容语义。
    """
    path = workspace_root / ".boxteam" / "state" / "workspace.sqlite"
    database = SQLiteStateDatabase(
        path=path,
        schema_version=13,
        migrations=store_module._WORKSPACE_MIGRATIONS[:13],
    )
    try:
        connection = database.connection()
        try:
            connection.execute(
                "CREATE TABLE workspace_config ("
                "config_key TEXT PRIMARY KEY,"
                "config_version INTEGER NOT NULL,"
                "payload_json TEXT NOT NULL,"
                "updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO workspace_config("
                "config_key, config_version, payload_json, updated_at) "
                "VALUES (?, 1, ?, '2026-01-01T00:00:00+00:00')",
                (config_key, json.dumps(payload)),
            )
            # 确保只有 wc 行、无对应 layer 行。
            connection.execute(
                "DELETE FROM config_source_layers WHERE config_key = ?",
                (config_key,),
            )
        finally:
            connection.close()
    finally:
        database.close()

def test_legacy_workspace_config_only_row_migrates_to_authoritative_layer(workspace_root):
    """仅有 legacy ``workspace_config`` 行的旧库，必须还原为等价 layer 语义后删表。

    如果迁移丢掉这行，运行时覆盖读将从旧表切到权威 layer 表时整体丢失配置。
    """
    _write_legacy_workspace_config_only_row(
        workspace_root, "workspace_runtime_override", {"ui": {"theme": "dark"}}
    )
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        layer = store.get_source_layer("workspace_runtime_override")
        assert layer is not None
        assert layer.presence == "present"
        assert layer.payload == {"ui": {"theme": "dark"}}
        assert layer.layer_revision == 1
        assert layer.source_generation == 1
        connection = store.connection()
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        assert "workspace_config" not in tables
    finally:
        store.close()

def test_legacy_workspace_config_only_row_preserves_runtime_override_keys(workspace_root, tmp_path):
    """回归保护 T-1 证据 3：只有 wc 行的旧库，``ConfigService`` 仍须读到运行时覆盖。

    迁移前该读依赖 legacy 表；迁移后必须改由权威 layer 行承载，结果仍为 ('ui',)。
    """
    from app.services.infrastructure.config_service import ConfigService

    _write_legacy_workspace_config_only_row(
        workspace_root, "workspace_runtime_override", {"ui": {"theme": "dark"}}
    )
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps({"config_version": 1}), encoding="utf-8")
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert service.get_runtime_override_keys() == ("ui",)
    finally:
        store.close()

def test_workspace_config_read_reports_corrupt_payload_with_context(workspace_root):
    """权威 layer 行被外部写成非法 JSON / 非对象时，读取必须带上下文响亮失败。"""

    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        store.sync_config_source(
            config_key="user",
            uri=None,
            config_version=1,
            presence="present",
            payload={"a": 1},
            layer_digest="digest-a",
        )
        assert store.get_source_layer("user").payload == {"a": 1}
        # 缺失是「从未保存」，返回 None；损坏是错误，必须抛出且带位置
        assert store.get_source_layer("never-saved") is None

        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_source_layers SET payload_json = 'not-json' "
                "WHERE config_key = 'user'"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ValueError, match="source layer payload"):
            store.get_source_layer("user")

        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_source_layers SET payload_json = '[1, 2]' "
                "WHERE config_key = 'user'"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(TypeError, match="source layer payload"):
            store.get_source_layer("user")
    finally:
        store.close()

def test_legacy_workspace_secret_migration_blocks_literal_and_normalizes_env(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        connection = store.connection()
        try:
            connection.execute(
                """
                INSERT INTO config_source_layers(
                    config_key, uri, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json
                ) VALUES ('legacy-literal', NULL, 'present', 1, ?, 1, 'd1', 1, NULL,
                          '2026-01-01T00:00:00+00:00', NULL)
                """,
                (json.dumps({"api_key": "literal-secret"}),),
            )
            connection.execute(
                """
                INSERT INTO config_source_layers(
                    config_key, uri, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json
                ) VALUES ('legacy-env', NULL, 'present', 1, ?, 1, 'd2', 1, NULL,
                          '2026-01-01T00:00:00+00:00', NULL)
                """,
                (json.dumps({"api_key": "${ROTATED_API_KEY}"}),),
            )
            connection.commit()
        finally:
            connection.close()

        blocked = store.migrate_legacy_config_secrets("legacy-literal")
        assert blocked == ()
        literal_record = store.get_source_layer("legacy-literal")
        assert literal_record is not None
        assert literal_record.payload["api_key"] == "literal-secret"

        assert store.migrate_legacy_config_secrets("legacy-env") == ()
        env_record = store.get_source_layer("legacy-env")
        assert env_record is not None
        assert env_record.payload == {"api_key": "env:ROTATED_API_KEY"}
    finally:
        store.close()

def test_legacy_workspace_secret_migration_blocks_irreversible_digest(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        connection = store.connection()
        try:
            connection.execute(
                """
                INSERT INTO config_source_layers(
                    config_key, uri, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json
                ) VALUES ('legacy-digest', NULL, 'present', 1, ?, 1, 'd3', 1, NULL,
                          '2026-01-01T00:00:00+00:00', NULL)
                """,
                (json.dumps({"api_key": "literal-sha256:deadbeef"}),),
            )
            connection.commit()
        finally:
            connection.close()

        blocked = store.migrate_legacy_config_secrets("legacy-digest")
        assert blocked == ("/api_key",)
        digest_record = store.get_source_layer("legacy-digest")
        assert digest_record is not None
        assert digest_record.payload["api_key"] == "literal-sha256:deadbeef"
    finally:
        store.close()

def test_secret_binding_resolver_reports_failure_and_detects_rotation(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ROTATING_API_KEY", raising=False)
    with pytest.raises(SecretResolutionError, match="无法解析"):
        build_secret_binding_summary(
            {"api_key": "env:ROTATING_API_KEY"},
            resolve_environment=True,
        )

    monkeypatch.setenv("ROTATING_API_KEY", "first-secret")
    first = build_secret_binding_summary(
        {"api_key": "env:ROTATING_API_KEY"},
        resolve_environment=True,
    )
    monkeypatch.setenv("ROTATING_API_KEY", "second-secret")
    second = build_secret_binding_summary(
        {"api_key": "env:ROTATING_API_KEY"},
        resolve_environment=True,
    )
    assert first["/api_key"]["secret_ref"] == "env:ROTATING_API_KEY"
    assert first["/api_key"]["binding_digest"] != second["/api_key"]["binding_digest"]
    assert "first-secret" not in json.dumps(first)
    assert "second-secret" not in json.dumps(second)

def test_source_journal_distinguishes_a_b_a_and_deduplicates_a_a(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = store.append_config_source_journal(
            source_key="user",
            source_event_id="source-event-1",
            uri=None,
            presence="present",
            layer_revision=1,
            layer_digest="digest-a",
            previous_digest=None,
            origin="file-watcher",
            fanout_id="fanout-1",
        )
        duplicate = store.append_config_source_journal(
            source_key="user",
            source_event_id="source-event-duplicate",
            uri=None,
            presence="present",
            layer_revision=1,
            layer_digest="digest-a",
            previous_digest=None,
            origin="file-watcher",
            fanout_id="fanout-duplicate",
            expected_source_generation=1,
        )
        assert duplicate.source_generation == first.source_generation
        second = store.append_config_source_journal(
            source_key="user",
            source_event_id="source-event-2",
            uri=None,
            presence="present",
            layer_revision=2,
            layer_digest="digest-b",
            previous_digest="digest-a",
            origin="api",
            fanout_id="fanout-2",
            expected_source_generation=1,
        )
        third = store.append_config_source_journal(
            source_key="user",
            source_event_id="source-event-3",
            uri=None,
            presence="present",
            layer_revision=3,
            layer_digest="digest-a",
            previous_digest="digest-b",
            origin="file-watcher",
            fanout_id="fanout-3",
            expected_source_generation=2,
        )
        assert (first.source_generation, second.source_generation, third.source_generation) == (
            1,
            2,
            3,
        )
        assert store.source_generation_high_water_mark(source_key="user") == 3
        assert [
            item.layer_digest
            for item in store.list_config_source_journal(source_key="user")
        ] == ["digest-a", "digest-b", "digest-a"]
    finally:
        store.close()

def test_source_journal_replays_stopped_workspace_and_reports_partial_fanout(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = store.append_config_source_journal(
            source_key="shared-user-workspace",
            source_event_id="shared-1",
            uri=None,
            presence="present",
            layer_revision=1,
            layer_digest="digest-a",
            previous_digest=None,
            origin="file-watcher",
            fanout_id="fanout:shared-user-workspace:event:shared-1",
        )
        second = store.append_config_source_journal(
            source_key="shared-user-workspace",
            source_event_id="shared-2",
            uri=None,
            presence="present",
            layer_revision=2,
            layer_digest="digest-b",
            previous_digest="digest-a",
            origin="api",
            fanout_id="fanout:shared-user-workspace:event:shared-2",
            expected_source_generation=first.source_generation,
        )
        replay = store.prepare_config_source_fanout(
            source_key="shared-user-workspace",
            workspace_id="workspace-b",
            after_generation=0,
        )
        assert [item["source_generation"] for item in replay] == [1, 2]

        store.record_config_source_fanout(
            source_key="shared-user-workspace",
            source_generation=first.source_generation,
            workspace_id="workspace-a",
            status="applied",
            layer_revision=1,
            layer_digest="digest-a",
            result="applied",
        )
        store.record_config_source_fanout(
            source_key="shared-user-workspace",
            source_generation=second.source_generation,
            workspace_id="workspace-a",
            status="conflict",
            result="conflict",
            error="source changed",
        )
        summary = store.config_source_fanout_summary(
            source_key="shared-user-workspace",
            source_generation=second.source_generation,
            workspace_ids=("workspace-a", "workspace-b"),
        )
        assert summary["result"] == "fanout_partial"
        assert summary["failed_workspace_ids"] == ("workspace-a",)
        assert summary["missing_workspace_ids"] == ()
        assert summary["pending_workspace_ids"] == ("workspace-b",)
    finally:
        store.close()
