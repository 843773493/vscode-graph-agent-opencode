from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
    ConfigEventInput,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def _store(tmp_path) -> WorkspaceStateStore:
    return WorkspaceStateStore(workspace_root=tmp_path / "workspace")


def _append(
    store: WorkspaceStateStore,
    event_id: str,
    *,
    config_domain: str = "workspace",
    idempotency_key: str | None = None,
    result: str = "applied",
    active_revision: int | None = 1,
    **overrides,
):
    payload = {
        "candidate_id": None,
        "attempt_id": None,
        "apply_id": None,
        "idempotency_key": idempotency_key,
        "commit_revision": None,
        "active_revision": active_revision,
        "pending_revision": None,
        "source": "watcher",
        "result": result,
    }
    payload.update(overrides)
    return store.append_config_event(
        event_id=event_id,
        config_domain=config_domain,
        **payload,
    )


def test_workspace_config_event_row_projection_preserves_every_column(tmp_path):
    """24 列行投影必须逐列还原，含三处共用 SELECT 的每一列。"""

    store = _store(tmp_path)
    try:
        created = store.append_config_event(
            event_id="cfg-full",
            config_domain="workspace",
            candidate_id="candidate-full",
            attempt_id="attempt-full",
            apply_id="apply-full",
            idempotency_key="reload-full",
            commit_revision=11,
            active_revision=12,
            pending_revision=13,
            source="api",
            result="restart_required",
            activation_scope="restart_workspace",
            changed_paths=("/logger", "/mcp"),
            applied_paths=("/logger",),
            deferred_paths=("/mcp",),
            error=None,
        )
        listed = store.list_config_events(config_domain="workspace")
        assert len(listed) == 1
        projected = listed[0]
        assert projected == created
        assert projected.event_seq == 1
        assert (projected.candidate_id, projected.attempt_id, projected.apply_id) == (
            "candidate-full",
            "attempt-full",
            "apply-full",
        )
        assert (projected.commit_revision, projected.active_revision, projected.pending_revision) == (
            11,
            12,
            13,
        )
        assert projected.changed_paths == ("/logger", "/mcp")
        assert projected.applied_paths == ("/logger",)
        assert projected.deferred_paths == ("/mcp",)
        assert projected.activation_scope == "restart_workspace"
        assert projected.error is None
        assert projected.relay_state == "pending"
        assert projected.relay_attempts == 0
        assert projected.relay_last_error is None
        assert projected.relay_claimed_by is None
        assert projected.relay_claimed_until is None
        # relay_next_attempt_at 有 NOT NULL DEFAULT 纪元值，append 不覆盖它
        assert projected.relay_next_attempt_at is not None
        assert projected.relay_next_attempt_at.year == 1970
        # occurred_at 必须来自本行写入时刻，而不是任何共享/固定值
        assert abs(datetime.now(UTC) - projected.occurred_at) < timedelta(minutes=5)
        row_occurred_at = store.connection().execute(
            "SELECT occurred_at FROM config_events WHERE event_id = 'cfg-full'"
        ).fetchone()[0]
        assert projected.occurred_at.isoformat() == row_occurred_at
    finally:
        store.close()


def test_workspace_config_event_outbox_relay_rejects_hijack_and_stale_finalize(tmp_path):
    store = _store(tmp_path)
    try:
        event = _append(store, "cfg-hijack")
        claimed = store.claim_config_event_relay(
            event_id=event.event_id, consumer_id="relay-a"
        )
        assert claimed is not None
        # 被 relay-a claim 的事件不能被 relay-b 确认或标记失败
        with pytest.raises(ConfigConflictError, match="不属于当前 consumer"):
            store.mark_config_event_relay_delivered(
                event_id=event.event_id, consumer_id="relay-b"
            )
        with pytest.raises(ConfigConflictError, match="不属于当前 consumer"):
            store.fail_config_event_relay(
                event_id=event.event_id, consumer_id="relay-b", error="hijack"
            )
        delivered = store.mark_config_event_relay_delivered(
            event_id=event.event_id, consumer_id="relay-a"
        )
        assert delivered.relay_state == "delivered"
        assert store.mark_config_event_relay_delivered(
            event_id=event.event_id, consumer_id="relay-a"
        ).relay_state == "delivered"
        # delivered 是全局幂等终态：第三方确认返回 delivered 而不报错
        assert store.mark_config_event_relay_delivered(
            event_id=event.event_id, consumer_id="relay-b"
        ).relay_state == "delivered"
    finally:
        store.close()


def test_workspace_config_event_relay_missing_event_and_argument_guards(tmp_path):
    store = _store(tmp_path)
    try:
        with pytest.raises(KeyError, match="不存在"):
            store.mark_config_event_relay_delivered(
                event_id="missing", consumer_id="relay"
            )
        with pytest.raises(KeyError, match="不存在"):
            store.fail_config_event_relay(
                event_id="missing", consumer_id="relay", error="x"
            )
        # consumer 账本路径先命中 rowcount==0，报归属冲突而非缺事件
        with pytest.raises(ConfigConflictError, match="不属于当前 consumer"):
            store.mark_config_event_delivered_for_consumer(
                event_id="missing", consumer_id="relay"
            )
        event = _append(store, "cfg-guards")
        with pytest.raises(ValueError, match="consumer_id 不能为空"):
            store.claim_config_event_relay(event_id=event.event_id, consumer_id=" ")
        with pytest.raises(ValueError, match="relay lease 必须大于 0"):
            store.claim_config_event_relay(
                event_id=event.event_id, consumer_id="relay", lease_seconds=0
            )
        with pytest.raises(ValueError, match="relay lease 必须大于 0"):
            store.claim_config_event_relay(
                event_id=event.event_id, consumer_id="relay", lease_seconds=True
            )
        with pytest.raises(ValueError, match="重试延迟不能为负数"):
            store.fail_config_event_relay(
                event_id=event.event_id,
                consumer_id="relay",
                error="x",
                retry_after_seconds=-1,
            )
        with pytest.raises(ValueError, match="不能为空"):
            store.fail_config_event_for_consumer(
                event_id=event.event_id, consumer_id="relay", error=" "
            )
        with pytest.raises(ValueError, match="分页参数无效"):
            store.list_config_events_for_relay(config_domain="workspace", limit=0)
        with pytest.raises(ValueError, match="分页参数无效"):
            store.claim_config_events_for_consumer(
                config_domain="workspace", after=-1, consumer_id="relay"
            )
        with pytest.raises(ValueError, match="consumer_id 不能为空"):
            store.claim_config_events_for_consumer(
                config_domain="workspace", after=0, consumer_id=" "
            )
        with pytest.raises(ValueError, match="relay lease 必须大于 0"):
            store.claim_config_events_for_consumer(
                config_domain="workspace",
                after=0,
                consumer_id="relay",
                lease_seconds=0,
            )
        with pytest.raises(ValueError, match="游标不能为负数"):
            store.ensure_config_event_cursor(config_domain="workspace", after=-1)
    finally:
        store.close()


def test_workspace_config_events_cross_domain_bounds_and_prune(tmp_path):
    """config_event_bounds / prune_config_events 按域隔离。"""

    store = _store(tmp_path)
    try:
        assert store.config_event_bounds(config_domain="workspace") == (None, 0)
        _append(store, "ws-1", config_domain="workspace")
        _append(store, "gw-1", config_domain="gateway")
        _append(store, "ws-2", config_domain="workspace")
        assert store.config_event_bounds(config_domain="workspace") == (1, 3)
        assert store.config_event_bounds(config_domain="gateway") == (2, 3)
        assert store.config_event_bounds(config_domain="unknown") == (None, 3)

        with pytest.raises(ValueError, match="保留天数必须大于 0"):
            store.prune_config_events(config_domain="workspace", retention_days=0)
        # 未来保留窗口不裁剪；网关域事件不受 workspace 域裁剪影响
        assert store.prune_config_events(config_domain="workspace", retention_days=3650) == 0
        assert [r.event_id for r in store.list_config_events(config_domain="gateway")] == [
            "gw-1"
        ]

        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_events SET occurred_at = ? WHERE event_id = 'ws-1'",
                ("2000-01-01T00:00:00+00:00",),
            )
            connection.commit()
        finally:
            connection.close()
        assert store.prune_config_events(config_domain="workspace", retention_days=30) == 1
        assert store.config_event_bounds(config_domain="workspace") == (3, 3)
    finally:
        store.close()


def test_workspace_config_events_reject_cursor_outside_retained_window(tmp_path):
    store = _store(tmp_path)
    try:
        for index in range(1, 5):
            _append(store, f"cfg-{index}")
        connection = sqlite3.connect(store.path)
        try:
            connection.execute("DELETE FROM config_events WHERE event_seq IN (1, 2)")
            connection.commit()
        finally:
            connection.close()
        assert store.config_event_bounds(config_domain="workspace") == (3, 4)
        # 连续窗口边界：after 恰好等于 first-1 时正常返回
        assert [r.event_seq for r in store.list_config_events(
            config_domain="workspace", after=2
        )] == [3, 4]
        with pytest.raises(ConfigEventCursorGoneError):
            store.list_config_events(config_domain="workspace", after=1)
    finally:
        store.close()


def test_workspace_config_events_reject_cursor_after_full_domain_prune(tmp_path):
    """整域事件被裁空后，越界游标必须报 CursorGone，不得静默返回空页。

    此时 first 为 None、sqlite_sequence 高水位仍在；若只看 first 会漏判，
    让调用方把「游标已失效」误当成「没有新事件」。
    """

    store = _store(tmp_path)
    try:
        for index in range(1, 6):
            _append(store, f"cfg-{index}")
        connection = sqlite3.connect(store.path)
        try:
            connection.execute("DELETE FROM config_events")
            connection.commit()
        finally:
            connection.close()
        assert store.config_event_bounds(config_domain="workspace") == (None, 5)
        # after=0 是全新订阅起点，允许正常返回空页
        assert store.list_config_events(config_domain="workspace", after=0) == ()
        with pytest.raises(ConfigEventCursorGoneError) as gone:
            store.list_config_events(config_domain="workspace", after=1)
        assert gone.value.first_available == 5
        with pytest.raises(ConfigEventCursorGoneError):
            store.ensure_config_event_cursor(config_domain="workspace", after=1)
    finally:
        store.close()


def test_workspace_config_event_consumer_ledger_is_independent(tmp_path):
    store = _store(tmp_path)
    try:
        event = _append(store, "cfg-consumer")
        first = store.claim_config_events_for_consumer(
            config_domain="workspace", after=0, consumer_id="consumer-a"
        )
        second = store.claim_config_events_for_consumer(
            config_domain="workspace", after=0, consumer_id="consumer-b"
        )
        assert [r.event_id for r in first] == [event.event_id]
        assert [r.event_id for r in second] == [event.event_id]
        store.mark_config_event_delivered_for_consumer(
            event_id=event.event_id, consumer_id="consumer-a"
        )
        assert [
            r.event_id
            for r in store.claim_config_events_for_consumer(
                config_domain="workspace", after=0, consumer_id="consumer-a"
            )
        ] == []
        # consumer-a 的确认不影响 consumer-b 的独立账本（仍为 claimed）
        ledger = {
            row[0]: (row[1], row[2], row[3])
            for row in store.connection().execute(
                "SELECT consumer_id, state, attempts, last_error "
                "FROM config_event_relay_delivery"
            )
        }
        assert ledger["consumer-a"][0] == "delivered"
        assert ledger["consumer-b"][0] == "claimed"
        # consumer-b 失败只改自己的账本行；全局 outbox relay 状态保持不变
        returned = store.fail_config_event_for_consumer(
            event_id=event.event_id, consumer_id="consumer-b", error="client gone"
        )
        assert returned.event_id == event.event_id
        assert returned.relay_state == "pending"
        failed_row = store.connection().execute(
            "SELECT state, last_error FROM config_event_relay_delivery "
            "WHERE consumer_id = 'consumer-b'"
        ).fetchone()
        assert (failed_row[0], failed_row[1]) == ("failed", "client gone")
        # consumer 未 claim 时不能标记失败
        with pytest.raises(ConfigConflictError, match="不属于当前 consumer"):
            store.fail_config_event_for_consumer(
                event_id=event.event_id, consumer_id="consumer-unclaimed", error="x"
            )
    finally:
        store.close()


def test_workspace_config_event_row_projection_rejects_non_string_paths(tmp_path):
    store = _store(tmp_path)
    try:
        _append(store, "cfg-corrupt")
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_events SET changed_paths_json = ? WHERE event_id = 'cfg-corrupt'",
                (json.dumps([1, 2]),),
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ValueError, match="必须是字符串数组"):
            store.list_config_events(config_domain="workspace")
    finally:
        store.close()


def test_workspace_config_event_insert_is_idempotent_by_key_and_event_id(tmp_path):
    store = _store(tmp_path)
    try:
        first = _append(store, "cfg-a", idempotency_key="reload-idem")
        same_key = _append(store, "cfg-b", idempotency_key="reload-idem")
        assert same_key.event_seq == first.event_seq
        assert same_key.event_id == first.event_id

        # 直接走 ConfigEventInput 的等价路径（其它族事务内复用的唯一插入实现）
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            direct = store._insert_config_event(
                connection,
                ConfigEventInput(
                    event_id="cfg-direct",
                    config_domain="workspace",
                    candidate_id=None,
                    attempt_id=None,
                    apply_id=None,
                    idempotency_key="direct-key",
                    commit_revision=7,
                    active_revision=7,
                    pending_revision=None,
                    source="api",
                    result="applied",
                    activation_scope="current",
                    changed_paths=("/a", "/b"),
                    applied_paths=("/a",),
                    deferred_paths=(),
                    error=None,
                ),
            )
            connection.execute("COMMIT")
        assert direct.changed_paths == ("/a", "/b")
        assert direct.applied_paths == ("/a",)
        assert direct.activation_scope == "current"
        assert direct.commit_revision == 7
        # 同 event_id 重放返回同一行
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = store._insert_config_event(
                connection,
                ConfigEventInput(
                    event_id="cfg-direct",
                    config_domain="workspace",
                    candidate_id=None,
                    attempt_id=None,
                    apply_id=None,
                    idempotency_key=None,
                    commit_revision=None,
                    active_revision=None,
                    pending_revision=None,
                    source="api",
                    result="applied",
                ),
            )
            connection.execute("COMMIT")
        assert replay == direct
    finally:
        store.close()


def test_workspace_config_event_relay_retry_backoff_reclaims_expired_claim(tmp_path):
    store = _store(tmp_path)
    try:
        event = _append(store, "cfg-expired")
        claimed = store.claim_config_event_relay(
            event_id=event.event_id, consumer_id="relay", lease_seconds=300
        )
        assert claimed is not None
        # 未过期时其它 relay 不能抢占
        assert (
            store.claim_config_event_relay(
                event_id=event.event_id, consumer_id="other"
            )
            is None
        )
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_events SET relay_state = 'claimed', relay_claimed_by = 'relay', "
                "relay_claimed_until = ? WHERE event_id = ?",
                ("2000-01-01T00:00:00+00:00", event.event_id),
            )
            connection.commit()
        finally:
            connection.close()
        reclaimed = store.claim_config_event_relay(
            event_id=event.event_id, consumer_id="other"
        )
        assert reclaimed is not None
        assert reclaimed.relay_claimed_by == "other"
        assert reclaimed.relay_attempts == 2
    finally:
        store.close()


def test_workspace_relay_claimed_with_null_lease_is_reclaimable(tmp_path):
    """claim 租约列为 NULL 时不能被永丢弃，必须可被恢复者接管。"""

    store = _store(tmp_path)
    try:
        event = _append(store, "cfg-null-lease")
        connection = sqlite3.connect(store.path)
        try:
            # 模拟异常中断：状态是 claimed 但没有租约时间（列可空）
            connection.execute(
                "UPDATE config_events SET relay_state = 'claimed', "
                "relay_claimed_until = NULL, relay_claimed_by = 'dead-relay'"
            )
            connection.commit()
        finally:
            connection.close()
        # outbox relay 读取必须把该行当作可投递，不能静默丢弃
        assert [
            item.event_id
            for item in store.list_config_events_for_relay(config_domain="workspace")
        ] == [event.event_id]
        # 单事件 claim 也必须能接管
        reclaimed = store.claim_config_event_relay(
            event_id=event.event_id, consumer_id="recovery-relay"
        )
        assert reclaimed is not None
        assert reclaimed.relay_claimed_by == "recovery-relay"
    finally:
        store.close()


def test_workspace_consumer_ledger_claimed_with_null_lease_is_reclaimable(tmp_path):
    """consumer 账本 claim 租约为 NULL 时也必须可被同一 consumer 重新认领。"""

    store = _store(tmp_path)
    try:
        event = _append(store, "cfg-ledger-null-lease")
        store.claim_config_events_for_consumer(
            config_domain="workspace", after=0, consumer_id="consumer-a"
        )
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_event_relay_delivery SET state = 'claimed', "
                "claimed_until = NULL WHERE consumer_id = 'consumer-a'"
            )
            connection.commit()
        finally:
            connection.close()
        reclaimed = store.claim_config_events_for_consumer(
            config_domain="workspace", after=0, consumer_id="consumer-a"
        )
        assert [record.event_id for record in reclaimed] == [event.event_id]
    finally:
        store.close()


def test_workspace_expired_consumer_claims_are_swept_on_reconnect(tmp_path):
    """SSE 重连遗留的过期 consumer claim 账本行必须被回收，delivered 必须保留。"""

    store = _store(tmp_path)
    try:
        event = _append(store, "cfg-sweep")
        store.claim_config_events_for_consumer(
            config_domain="workspace",
            after=0,
            consumer_id="consumer-old",
            lease_seconds=0.001,
        )
        # 让 consumer-done 有一行 delivered 终态：先 claim 再确认
        store.claim_config_events_for_consumer(
            config_domain="workspace", after=0, consumer_id="consumer-done"
        )
        store.mark_config_event_delivered_for_consumer(
            event_id=event.event_id, consumer_id="consumer-done"
        )
        # 新 consumer 认领时顺带回收过期 claim，但 delivered 终态保留
        store.claim_config_events_for_consumer(
            config_domain="workspace", after=0, consumer_id="consumer-new"
        )
        rows = dict(
            store.connection()
            .execute(
                "SELECT consumer_id, state FROM config_event_relay_delivery"
            )
            .fetchall()
        )
        assert "consumer-old" not in rows
        assert rows["consumer-done"] == "delivered"
        assert rows["consumer-new"] == "claimed"
    finally:
        store.close()


def test_workspace_config_event_bad_path_row_error_carries_locator(tmp_path):
    """坏路径行的报错必须携带 event_seq/event_id，避免整页读取无从诊断。"""

    store = _store(tmp_path)
    try:
        _append(store, "cfg-good")
        _append(store, "cfg-bad")
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE config_events SET changed_paths_json = 'not-json' "
                "WHERE event_id = 'cfg-bad'"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ValueError, match="event_seq=2, event_id=cfg-bad"):
            store.list_config_events(config_domain="workspace")
    finally:
        store.close()
