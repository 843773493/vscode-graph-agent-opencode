from __future__ import annotations

import pytest

from app.services.infrastructure.config.state import (
    ConfigEventCursorGoneError,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def test_workspace_config_events_are_durable_replayable_and_idempotent(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = store.append_config_event(
            event_id="config-event-1",
            config_domain="workspace",
            candidate_id="candidate-1",
            attempt_id="attempt-1",
            apply_id=None,
            idempotency_key="reload-1",
            commit_revision=None,
            active_revision=1,
            pending_revision=2,
            source="watcher",
            result="restart_required",
            activation_scope="restart_workspace",
            changed_paths=("/logger",),
            deferred_paths=("/logger",),
        )
        duplicate = store.append_config_event(
            event_id="config-event-1",
            config_domain="workspace",
            candidate_id="candidate-other",
            attempt_id="attempt-other",
            apply_id="apply-other",
            idempotency_key="reload-other",
            commit_revision=3,
            active_revision=3,
            pending_revision=None,
            source="api",
            result="applied",
        )
        assert duplicate == first
        replay = store.list_config_events(config_domain="workspace", after=0)
        assert replay == (first,)
        assert replay[0].event_seq == 1
        assert replay[0].deferred_paths == ("/logger",)
        assert replay[0].activation_scope == "restart_workspace"
    finally:
        store.close()

def test_workspace_config_event_outbox_claim_retry_and_dedup(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        created = store.append_config_event(
            event_id="config-outbox-1",
            config_domain="workspace",
            candidate_id="candidate-1",
            attempt_id="attempt-1",
            apply_id="apply-1",
            idempotency_key="reload-outbox-1",
            commit_revision=None,
            active_revision=1,
            pending_revision=2,
            source="watcher",
            result="restart_required",
            activation_scope="restart_workspace",
        )
        assert created.relay_state == "pending"
        assert created.relay_attempts == 0

        claimed = store.claim_config_event_relay(
            event_id=created.event_id,
            consumer_id="sse-relay",
        )
        assert claimed is not None
        assert claimed.relay_state == "claimed"
        assert claimed.relay_attempts == 1
        assert store.claim_config_event_relay(
            event_id=created.event_id,
            consumer_id="other-relay",
        ) is None

        failed = store.fail_config_event_relay(
            event_id=created.event_id,
            consumer_id="sse-relay",
            error="client disconnected",
        )
        assert failed.relay_state == "failed"
        assert failed.relay_last_error == "client disconnected"
        retried = store.claim_config_event_relay(
            event_id=created.event_id,
            consumer_id="sse-relay",
        )
        assert retried is not None
        assert retried.event_seq == created.event_seq
        assert retried.relay_attempts == 2

        delivered = store.mark_config_event_relay_delivered(
            event_id=created.event_id,
            consumer_id="sse-relay",
        )
        assert delivered.relay_state == "delivered"
        assert store.mark_config_event_relay_delivered(
            event_id=created.event_id,
            consumer_id="different-consumer",
        ).event_seq == created.event_seq
        assert store.list_config_events_for_relay(
            config_domain="workspace"
        ) == ()
    finally:
        store.close()

def test_workspace_config_event_relay_is_independent_per_consumer(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        event = store.append_config_event(
            event_id="config-consumer-independent",
            config_domain="workspace",
            candidate_id=None,
            attempt_id=None,
            apply_id=None,
            idempotency_key=None,
            commit_revision=None,
            active_revision=1,
            pending_revision=None,
            source="watcher",
            result="applied",
        )
        first = store.claim_config_events_for_consumer(
            config_domain="workspace",
            after=0,
            consumer_id="consumer-a",
        )
        second = store.claim_config_events_for_consumer(
            config_domain="workspace",
            after=0,
            consumer_id="consumer-b",
        )
        assert [item.event_id for item in first] == [event.event_id]
        assert [item.event_id for item in second] == [event.event_id]
        store.mark_config_event_delivered_for_consumer(
            event_id=event.event_id,
            consumer_id="consumer-a",
        )
        assert store.claim_config_events_for_consumer(
            config_domain="workspace",
            after=0,
            consumer_id="consumer-a",
        ) == ()
        assert store.claim_config_events_for_consumer(
            config_domain="workspace",
            after=0,
            consumer_id="consumer-b",
        ) == ()
    finally:
        store.close()

def test_workspace_config_event_cursor_reports_snapshot_required(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        for index in range(3):
            store.append_config_event(
                event_id=f"event-{index}",
                config_domain="workspace",
                candidate_id=None,
                attempt_id=None,
                apply_id=None,
                idempotency_key=None,
                commit_revision=None,
                active_revision=None,
                pending_revision=None,
                source="test",
                result="unchanged",
            )
        connection = store.connection()
        try:
            connection.execute(
                "DELETE FROM config_events WHERE event_seq IN (1, 2)"
            )
        finally:
            connection.close()
        with pytest.raises(ConfigEventCursorGoneError):
            store.list_config_events(config_domain="workspace", after=1)
    finally:
        store.close()
