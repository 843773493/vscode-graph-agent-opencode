from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def _store(tmp_path) -> WorkspaceStateStore:
    return WorkspaceStateStore(workspace_root=tmp_path / "workspace")


def _journal(
    store: WorkspaceStateStore,
    *,
    source_key: str = "user",
    event_id: str = "ev1",
    layer_revision: int = 1,
    layer_digest: str | None = "d1",
    previous_digest: str | None = None,
    presence: str = "present",
    origin: str = "file-watcher",
    fanout_id: str | None = None,
    expected: int | None = None,
    source_path: Path | None = None,
):
    return store.append_config_source_journal(
        source_key=source_key,
        source_event_id=event_id,
        source_path=source_path or Path("/tmp/ws/workspace.jsonc"),
        presence=presence,
        layer_revision=layer_revision,
        layer_digest=layer_digest,
        previous_digest=previous_digest,
        origin=origin,
        fanout_id=fanout_id or f"fanout:{source_key}:{layer_revision}",
        expected_source_generation=expected,
    )


def test_journal_row_projection_preserves_every_column(tmp_path):
    store = _store(tmp_path)
    try:
        created = _journal(
            store,
            event_id="ev-full",
            layer_revision=4,
            layer_digest="digest-full",
            previous_digest="digest-prev",
            presence="present",
            origin="api",
            fanout_id="fanout-full",
            source_path=tmp_path / "workspace.jsonc",
        )
        listed = store.list_config_source_journal(source_key="user")
        assert len(listed) == 1
        projected = listed[0]
        assert projected == created
        assert (projected.source_key, projected.source_generation) == ("user", 1)
        assert projected.source_event_id == "ev-full"
        assert projected.source_path == str((tmp_path / "workspace.jsonc").resolve())
        assert projected.layer_revision == 4
        assert projected.layer_digest == "digest-full"
        assert projected.previous_digest == "digest-prev"
        assert projected.origin == "api"
        assert projected.fanout_id == "fanout-full"
        assert isinstance(projected.created_at, datetime)
        # created_at 必须来自本行写入时刻，而非任何共享/固定值
        row_value = store.connection().execute(
            "SELECT created_at FROM config_source_journal WHERE source_event_id = 'ev-full'"
        ).fetchone()[0]
        assert projected.created_at.isoformat() == row_value
    finally:
        store.close()


def test_journal_distinguishes_a_b_a_and_deduplicates_a_a(tmp_path):
    store = _store(tmp_path)
    try:
        first = _journal(store, event_id="ev1", layer_revision=1, layer_digest="a")
        dup = _journal(
            store, event_id="ev-dup", layer_revision=1, layer_digest="a", expected=1
        )
        assert dup.source_generation == first.source_generation
        second = _journal(
            store, event_id="ev2", layer_revision=2, layer_digest="b",
            previous_digest="a", origin="api", expected=1,
        )
        third = _journal(
            store, event_id="ev3", layer_revision=3, layer_digest="a",
            previous_digest="b", expected=2,
        )
        assert (first.source_generation, second.source_generation, third.source_generation) == (
            1, 2, 3,
        )
        assert store.source_generation_high_water_mark(source_key="user") == 3
        assert [
            item.layer_digest for item in store.list_config_source_journal(source_key="user")
        ] == ["a", "b", "a"]
    finally:
        store.close()


def test_journal_event_id_replay_is_idempotent(tmp_path):
    """同 event_id 重放返回同一条记录，不新增 generation。"""

    store = _store(tmp_path)
    try:
        first = _journal(store, event_id="ev1", layer_revision=1, layer_digest="a")
        replay = _journal(store, event_id="ev1", layer_revision=1, layer_digest="a")
        assert replay == first
        assert store.source_generation_high_water_mark(source_key="user") == 1
        assert len(store.list_config_source_journal(source_key="user")) == 1
    finally:
        store.close()


def test_journal_event_id_rebind_differs_between_public_and_in_transaction_paths(tmp_path):
    """锁定 event_id 重绑在两个入口上的既有语义差异（非本轮引入）。

    公开入口对同 event_id 的不同 source 记录宽松返回旧行；事务内辅助严格报冲突。
    两者行为不同，本测试把两条语义都钉住，避免后续误改造成静默漂移。
    """

    store = _store(tmp_path)
    try:
        original = _journal(store, source_key="user", event_id="ev1", layer_revision=1,
                            layer_digest="a")
        lenient = _journal(store, source_key="other", event_id="ev1", layer_revision=7,
                           layer_digest="z")
        assert lenient == original

        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(ConfigConflictError, match="已绑定不同 source 记录"):
                    store._append_config_source_journal_in_connection(
                        connection,
                        source_key="other",
                        source_event_id="ev1",
                        source_path=Path("/tmp/other.jsonc"),
                        presence="present",
                        layer_revision=7,
                        layer_digest="z",
                        previous_digest=None,
                        origin="api",
                        fanout_id="fanout-other",
                    )
            finally:
                connection.execute("ROLLBACK")
    finally:
        store.close()


def test_journal_rejects_invalid_inputs_and_cas(tmp_path):
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="presence"):
            _journal(store, event_id="ev-bad", presence="maybe")
        with pytest.raises(ValueError, match="身份不能为空"):
            _journal(store, source_key="", event_id="ev-empty")

        _journal(store, event_id="ev1", layer_revision=1, layer_digest="a")
        with pytest.raises(ConfigConflictError, match="generation CAS"):
            _journal(store, event_id="ev-cas", layer_revision=5, layer_digest="b",
                     expected=99)
        with pytest.raises(ValueError, match="分页参数无效"):
            store.list_config_source_journal(source_key="user", after_generation=-1)
        with pytest.raises(ValueError, match="分页参数无效"):
            store.list_config_source_journal(source_key="user", limit=0)
        with pytest.raises(ValueError, match="分页参数无效"):
            store.list_config_source_journal(source_key="user", limit=2001)
    finally:
        store.close()


def test_journal_row_projection_preserves_absent_presence(tmp_path):
    """行工厂必须还原 presence 列本身，而非任何固定值。"""

    store = _store(tmp_path)
    try:
        created = _journal(
            store, event_id="ev-absent", layer_revision=1, layer_digest=None,
            presence="absent",
        )
        assert created.presence == "absent"
        projected = store.list_config_source_journal(source_key="user")[0]
        assert projected.presence == "absent"
        assert projected.layer_digest is None
    finally:
        store.close()


def test_in_transaction_journal_helper_dedup_and_cas(tmp_path):
    """直接钉住事务内辅助的去重与 generation CAS 分支。"""

    store = _store(tmp_path)
    try:
        _journal(store, event_id="ev1", layer_revision=1, layer_digest="a")

        def call_helper(**overrides):
            kwargs = {
                "source_key": "user",
                "source_event_id": "ev-helper",
                "source_path": Path("/tmp/helper.jsonc"),
                "presence": "present",
                "layer_revision": 1,
                "layer_digest": "a",
                "previous_digest": "a",
                "origin": "api",
                "fanout_id": "fanout-helper",
                "expected_source_generation": None,
            }
            kwargs.update(overrides)
            with store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    return store._append_config_source_journal_in_connection(
                        connection, **kwargs
                    )
                finally:
                    connection.execute("ROLLBACK")

        # 同 presence + 同 digest：A-A 去重，返回既有 generation，不新增
        assert call_helper() == 1
        # digest 不同：必须新增 generation（m4 把去重条件改成恒真时会误判为去重）
        assert call_helper(source_event_id="ev-helper-2", layer_digest="b") == 2
        # generation CAS：期望值不符必须报冲突（m5 关掉 CAS 时会静默新建）
        with pytest.raises(ConfigConflictError, match="generation CAS 冲突"):
            call_helper(
                source_event_id="ev-helper-3", layer_digest="c",
                expected_source_generation=99,
            )
    finally:
        store.close()


def test_in_transaction_journal_helper_rejects_source_key_rebind_alone(tmp_path):
    """仅 source_key 不同（其余列一致）也必须被拒绝：绑定守卫不能只靠其它列兜底。"""

    store = _store(tmp_path)
    try:
        _journal(store, source_key="user", event_id="ev1", layer_revision=1,
                 layer_digest="a", presence="present")
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(ConfigConflictError, match="已绑定不同 source 记录"):
                    store._append_config_source_journal_in_connection(
                        connection,
                        source_key="other",
                        source_event_id="ev1",
                        source_path=Path("/tmp/other.jsonc"),
                        presence="present",
                        layer_revision=1,
                        layer_digest="a",
                        previous_digest=None,
                        origin="api",
                        fanout_id="fanout-other",
                    )
            finally:
                connection.execute("ROLLBACK")
    finally:
        store.close()


def test_journal_list_pagination_and_unknown_high_water_mark(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.source_generation_high_water_mark(source_key="unknown") == 0
        assert store.list_config_source_journal(source_key="unknown") == ()
        for index in range(1, 4):
            _journal(store, event_id=f"ev{index}", layer_revision=index,
                     layer_digest=f"d{index}")
        assert [r.source_generation for r in store.list_config_source_journal(
            source_key="user", after_generation=1)] == [2, 3]
        assert len(store.list_config_source_journal(source_key="user", limit=1)) == 1
    finally:
        store.close()


def test_source_layer_sync_get_and_cas(tmp_path):
    store = _store(tmp_path)
    try:
        layer = store.sync_config_source(
            config_key="workspace",
            source_path=tmp_path / "workspace.jsonc",
            config_version=1,
            presence="present",
            payload={"logger": {"level": "info"}},
            layer_digest="ld1",
        )
        assert (layer.config_key, layer.layer_revision, layer.layer_digest) == (
            "workspace", 1, "ld1"
        )
        assert layer.payload == {"logger": {"level": "info"}}
        assert store.get_source_layer("missing") is None

        absent = store.sync_config_source(
            config_key="absent-key",
            source_path=tmp_path / "absent.jsonc",
            config_version=1,
            presence="absent",
            payload=None,
            layer_digest=None,
        )
        assert (absent.presence, absent.payload) == ("absent", None)

        with pytest.raises(ConfigConflictError, match="CAS"):
            store.sync_config_source(
                config_key="workspace",
                source_path=tmp_path / "workspace.jsonc",
                config_version=2,
                presence="present",
                payload={"logger": {"level": "warn"}},
                layer_digest="ld9",
                expected_layer_revision=99,
                expected_layer_digest="ld1",
            )
        with pytest.raises(ValueError, match="presence"):
            store.sync_config_source(
                config_key="workspace", source_path=tmp_path / "w.jsonc",
                config_version=1, presence="maybe", payload={"a": 1}, layer_digest="d",
            )
        with pytest.raises(ValueError, match="必须有 payload"):
            store.sync_config_source(
                config_key="workspace", source_path=tmp_path / "w.jsonc",
                config_version=1, presence="present", payload=None, layer_digest="d",
            )
        with pytest.raises(ValueError, match="必须为空"):
            store.sync_config_source(
                config_key="workspace", source_path=tmp_path / "w.jsonc",
                config_version=1, presence="absent", payload={"a": 1}, layer_digest=None,
            )
        with pytest.raises(ValueError, match="active CAS 必须同时"):
            store.sync_config_source(
                config_key="workspace", source_path=tmp_path / "w.jsonc",
                config_version=1, presence="present", payload={"a": 1},
                layer_digest="d", expected_active_revision=1,
            )
        with pytest.raises(ValueError, match="config_domain"):
            store.sync_config_source(
                config_key="workspace", source_path=tmp_path / "w.jsonc",
                config_version=1, presence="present", payload={"a": 1},
                layer_digest="d", expected_active_revision=1, expected_active_digest="x",
            )
    finally:
        store.close()


def test_source_layer_corrupt_payload_is_reported(tmp_path):
    store = _store(tmp_path)
    try:
        store.sync_config_source(
            config_key="workspace", source_path=tmp_path / "w.jsonc",
            config_version=1, presence="present", payload={"a": 1}, layer_digest="ld1",
        )
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_source_layers SET payload_json = 'not-json' "
                "WHERE config_key = 'workspace'"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ValueError):
            store.get_source_layer("workspace")
    finally:
        store.close()


def test_update_source_generation_binds_layer_to_shared_owner(tmp_path):
    store = _store(tmp_path)
    try:
        store.sync_config_source(
            config_key="workspace", source_path=tmp_path / "w.jsonc",
            config_version=1, presence="present", payload={"a": 1}, layer_digest="ld1",
        )
        current = store.get_source_layer("workspace")
        updated = store.update_source_generation(
            config_key="workspace", source_generation=1,
            expected_layer_revision=current.layer_revision,
            expected_layer_digest=current.layer_digest,
        )
        assert updated.source_generation == 1
        with pytest.raises(ConfigConflictError, match="不存在"):
            store.update_source_generation(
                config_key="missing", source_generation=1,
                expected_layer_revision=1, expected_layer_digest="d",
            )
        with pytest.raises(ValueError, match="必须为正数"):
            store.update_source_generation(
                config_key="workspace", source_generation=0,
                expected_layer_revision=1, expected_layer_digest="d",
            )
    finally:
        store.close()


def test_fanout_prepare_record_list_and_summary_states(tmp_path):
    store = _store(tmp_path)
    try:
        for index in range(1, 4):
            _journal(store, event_id=f"ev{index}", layer_revision=index,
                     layer_digest=f"d{index}")
        prepared = store.prepare_config_source_fanout(
            source_key="user", workspace_id="ws-a", after_generation=0
        )
        assert [item["source_generation"] for item in prepared] == [1, 2, 3]
        assert all(item["status"] == "pending" for item in prepared)
        # 重复 prepare 幂等：仍保持 pending
        assert len(store.prepare_config_source_fanout(
            source_key="user", workspace_id="ws-a", after_generation=0
        )) == 3

        store.record_config_source_fanout(
            source_key="user", source_generation=1, workspace_id="ws-a",
            status="applied", layer_revision=1, layer_digest="d1", result="applied",
        )
        listed = store.list_config_source_fanout(source_key="user", source_generation=1)
        assert [(item["workspace_id"], item["status"]) for item in listed] == [
            ("ws-a", "applied")
        ]
        assert store.config_source_fanout_summary(
            source_key="user", source_generation=1, workspace_ids=("ws-a",)
        )["result"] == "applied"

        store.record_config_source_fanout(
            source_key="user", source_generation=3, workspace_id="ws-a",
            status="conflict", result="conflict", error="source changed",
        )
        partial = store.config_source_fanout_summary(
            source_key="user", source_generation=3, workspace_ids=("ws-a", "ws-b")
        )
        assert partial["result"] == "fanout_partial"
        assert partial["failed_workspace_ids"] == ("ws-a",)
        assert partial["missing_workspace_ids"] == ("ws-b",)

        with pytest.raises(ValueError, match="不能为空且不能重复"):
            store.config_source_fanout_summary(
                source_key="user", source_generation=1, workspace_ids=("ws-a", "ws-a")
            )
        with pytest.raises(ValueError, match="不能为空"):
            store.config_source_fanout_summary(
                source_key="user", source_generation=1, workspace_ids=()
            )
        with pytest.raises(ConfigConflictError, match="不存在"):
            store.record_config_source_fanout(
                source_key="user", source_generation=99, workspace_id="ws-a",
                status="applied",
            )
        with pytest.raises(ValueError, match="工作区和状态不能为空"):
            store.record_config_source_fanout(
                source_key="user", source_generation=1, workspace_id="", status="applied"
            )
        with pytest.raises(ValueError, match="必须为正数"):
            store.record_config_source_fanout(
                source_key="user", source_generation=0, workspace_id="ws-a",
                status="applied",
            )
        with pytest.raises(ValueError, match="fan-out workspace"):
            store.prepare_config_source_fanout(source_key="user", workspace_id=" ")
    finally:
        store.close()


def test_fanout_prepare_reports_persisted_status_not_hardcoded_pending(tmp_path):
    """prepare 必须回读库中真实状态：既有 applied/conflict 行不得被谎报为 pending。"""

    store = _store(tmp_path)
    try:
        for index in range(1, 4):
            _journal(store, event_id=f"ev{index}", layer_revision=index,
                     layer_digest=f"d{index}")
        store.record_config_source_fanout(
            source_key="user", source_generation=1, workspace_id="ws-a",
            status="applied", layer_revision=1, layer_digest="d1", result="applied",
        )
        store.record_config_source_fanout(
            source_key="user", source_generation=2, workspace_id="ws-a",
            status="conflict", result="conflict", error="source changed",
        )
        returned = store.prepare_config_source_fanout(
            source_key="user", workspace_id="ws-a", after_generation=0
        )
        assert [(item["source_generation"], item["status"]) for item in returned] == [
            (1, "applied"),
            (2, "conflict"),
            (3, "pending"),
        ]
        # 另一 workspace 未导入过，全部为 pending
        assert {
            item["status"] for item in store.prepare_config_source_fanout(
                source_key="user", workspace_id="ws-b", after_generation=0
            )
        } == {"pending"}
    finally:
        store.close()


def test_fanout_prepare_rolls_back_all_generations_on_midloop_failure(tmp_path):
    """批量 prepare 必须整批原子：中途失败不得残留前几条已写入的行。"""

    store = _store(tmp_path)
    try:
        for index in range(1, 4):
            _journal(store, event_id=f"ev{index}", layer_revision=index,
                     layer_digest=f"d{index}")
        connection = sqlite3.connect(store.path, isolation_level=None)
        try:
            connection.execute(
                """
                CREATE TRIGGER fanout_fail_on_generation_two
                BEFORE INSERT ON config_source_fanout
                WHEN NEW.source_generation = 2
                BEGIN
                    SELECT RAISE(ABORT, 'injected fanout failure');
                END;
                """
            )
        finally:
            connection.close()

        # 第 2 条 INSERT 被触发器中止：整个批次必须回滚，第 1 条也不得残留
        with pytest.raises(sqlite3.IntegrityError, match="injected fanout failure"):
            store.prepare_config_source_fanout(
                source_key="user", workspace_id="ws-a", after_generation=0
            )
        assert store.list_config_source_fanout(
            source_key="user", source_generation=1
        ) == ()
        assert store.list_config_source_fanout(
            source_key="user", source_generation=3
        ) == ()
    finally:
        store.close()


def test_source_water_mark_rejects_missing_owner_with_existing_journal(tmp_path):
    """水位行被外部清空但 journal 仍有 generation 时必须响亮报错，不返回虚假 0。"""

    store = _store(tmp_path)
    try:
        _journal(store, event_id="ev1", layer_revision=1, layer_digest="d1")
        assert store.source_generation_high_water_mark(source_key="user") == 1
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "DELETE FROM config_source_owner WHERE source_key = 'user'"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(RuntimeError, match="水位缺失但 journal 已有 generation"):
            store.source_generation_high_water_mark(source_key="user")
    finally:
        store.close()


def test_source_water_mark_rejects_corrupt_owner_generation(tmp_path):
    """owner 水位被写成非法值（会算出负值高水位）时必须 fail-closed。"""

    store = _store(tmp_path)
    try:
        _journal(store, event_id="ev1", layer_revision=1, layer_digest="d1")
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_source_owner SET next_generation = 0 "
                "WHERE source_key = 'user'"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(RuntimeError, match="next_generation 非法"):
            store.source_generation_high_water_mark(source_key="user")
    finally:
        store.close()
