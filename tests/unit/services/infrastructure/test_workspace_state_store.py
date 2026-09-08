from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
    ConfigEventInput,
    SecretResolutionError,
    build_secret_binding_summary,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def test_workspace_state_uses_workspace_boundary_and_activity_cursor(tmp_path):
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        assert store.path == workspace_root / ".boxteam" / "state" / "workspace.sqlite"
        store.set_config(
            config_key="workspace",
            config_version=1,
            payload={"jobs": {"max_concurrency": 2}},
        )
        first = store.append_activity(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        second = store.append_activity(
            event_id="event-2",
            session_id="session-2",
            status="failed",
            summary="任务失败",
        )
        duplicate = store.append_activity(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        assert first.event_seq == 1
        assert duplicate.event_seq == first.event_seq
        assert [item.event_id for item in store.list_activity(after=first.event_seq)] == [
            second.event_id
        ]
        assert store.diagnostics().schema_version == 12
    finally:
        store.close()


def test_workspace_config_events_are_durable_replayable_and_idempotent(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
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


def test_workspace_apply_journal_records_idempotent_side_effect(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        store.start_config_apply_journal(
            config_domain="workspace",
            apply_id="apply-side-effect",
            candidate_id="candidate-side-effect",
            attempt_id="attempt-side-effect",
            owner="test",
            base_active_revision=1,
            pending_revision=2,
            source_baseline={},
            active_baseline={},
        )
        side_effect = {"resource": "logger", "action": "restart"}
        first = store.append_config_apply_side_effect(
            apply_id="apply-side-effect",
            side_effect=side_effect,
        )
        second = store.append_config_apply_side_effect(
            apply_id="apply-side-effect",
            side_effect=side_effect,
        )
        assert first.side_effects == (side_effect,)
        assert second.side_effects == (side_effect,)
        store.update_config_apply_journal(
            apply_id="apply-side-effect",
            expected_state="applying",
            state="committed",
        )
        with pytest.raises(ConfigConflictError, match="副作用追加状态 CAS"):
            store.append_config_apply_side_effect(
                apply_id="apply-side-effect",
                side_effect={"resource": "terminal", "action": "restart"},
            )
    finally:
        store.close()


def test_workspace_apply_journal_requires_explicit_compensation_result(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        store.start_config_apply_journal(
            config_domain="workspace",
            apply_id="apply-compensation",
            candidate_id="candidate-compensation",
            attempt_id="attempt-compensation",
            owner="test",
            base_active_revision=1,
            pending_revision=2,
            source_baseline={},
            active_baseline={},
        )
        store.append_config_apply_side_effect(
            apply_id="apply-compensation",
            side_effect={"resource": "logger", "action": "reload"},
        )
        failed = store.record_config_apply_compensation(
            apply_id="apply-compensation",
            expected_state="applying",
            compensation={
                "resource": "logger",
                "action": "restore",
                "status": "failed",
                "error": "旧 logger 无法恢复",
            },
        )
        assert failed.state == "recovery_required"
        assert failed.last_error == "旧 logger 无法恢复"

        compensated = store.record_config_apply_compensation(
            apply_id="apply-compensation",
            compensation={
                "resource": "logger",
                "action": "restore",
                "status": "succeeded",
            },
        )
        assert compensated.state == "compensated"
        assert [item["phase"] for item in compensated.side_effects if "phase" in item] == [
            "compensation",
            "compensation",
        ]
    finally:
        store.close()


def test_workspace_active_promotion_commits_apply_journal_atomically(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        active = store.ensure_active_config_snapshot(
            config_domain="workspace",
            payload={"config_version": 1},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            secret_bindings={},
            schema_version=1,
        )
        pending = store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-atomic-promotion",
            idempotency_key="reload:atomic-promotion",
            payload={"config_version": 2},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="candidate-digest",
            target_generation="workspace-runtime-2",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=active.active_revision,
            source_generation=0,
        )
        claim = store.begin_config_apply(
            config_domain="workspace",
            candidate_id=pending.candidate_id,
            attempt_id="attempt-atomic-promotion",
            apply_id="apply-atomic-promotion",
            owner="workspace-config-service",
            base_active_revision=active.active_revision,
            target_generation="workspace-runtime-2",
            pending_revision=pending.pending_revision,
            source_baseline={},
            active_baseline={},
        )

        promoted = store.promote_active_config_snapshot(
            config_domain="workspace",
            candidate_id=pending.candidate_id,
            payload={"config_version": 2},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="candidate-digest",
            secret_bindings={},
            schema_version=1,
            promoted_generation="workspace-runtime-2",
            promoted_apply_id=claim.apply_id,
            expected_active_revision=active.active_revision,
            expected_source_generation=0,
            expected_source_baseline={},
            expected_layer_revisions={},
            expected_layer_digests={},
            expected_pending_revision=pending.pending_revision,
            expected_pending_state="applying",
            expected_fencing_token=claim.fencing_token,
        )

        journal = store.get_config_apply_journal(apply_id=claim.apply_id)
        assert journal is not None
        assert journal.state == "committed"
        assert promoted.effective_digest == "candidate-digest"
        assert store.get_pending_config_candidate(
            config_domain="workspace", candidate_id=pending.candidate_id
        ).state == "active"
    finally:
        store.close()


def test_workspace_pending_candidate_persists_secret_binding_without_secret_value(
    tmp_path,
):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        pending = store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-secret-binding",
            idempotency_key="reload:secret-binding",
            payload={"config_version": 1, "api_key": "env:BOXTEAM_TEST_API_KEY"},
            source_baseline={},
            candidate_digest="candidate-secret-digest",
            effective_digest="candidate-secret-digest",
            target_generation="workspace-runtime-secret",
            fencing_token=None,
            state="candidate_validated",
            source_generation=1,
        )

        binding = pending.secret_bindings["/api_key"]
        assert binding["secret_ref"] == "env:BOXTEAM_TEST_API_KEY"
        assert binding["secret_version"]
        assert binding["binding_digest"]
        with store.connection() as connection:
            persisted = connection.execute(
                "SELECT payload_json, secret_bindings_json FROM config_pending_candidate"
            ).fetchone()
        assert "BOXTEAM_TEST_API_KEY" in persisted[0]
        assert "secret_bindings_json" not in str(persisted[0])
        assert "BOXTEAM_TEST_API_KEY" in persisted[1]
    finally:
        store.close()


def test_workspace_config_event_outbox_claim_retry_and_dedup(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
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


def test_workspace_pending_candidate_can_be_explicitly_discarded(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        pending = store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-discard",
            idempotency_key="reload:discard",
            payload={"config_version": 1},
            source_baseline={},
            candidate_digest="candidate-discard",
            effective_digest="candidate-discard",
            target_generation="workspace-runtime",
            fencing_token=None,
            state="candidate_validated",
        )
        discarded = store.update_pending_config_candidate_state(
            config_domain="workspace",
            candidate_id=pending.candidate_id,
            expected_state="candidate_validated",
            state="discarded",
            last_error="用户显式丢弃候选",
            event=ConfigEventInput(
                event_id="config:candidate-discard:discarded",
                config_domain="workspace",
                candidate_id=pending.candidate_id,
                attempt_id=None,
                apply_id=None,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=None,
                pending_revision=pending.pending_revision,
                source="test",
                result="discarded",
                activation_scope="restart_workspace",
                error="用户显式丢弃候选",
            ),
        )
        assert discarded.state == "discarded"
        assert store.get_pending_config_candidate(
            config_domain="workspace", candidate_id=pending.candidate_id
        ).state == "discarded"
        events = store.list_config_events(config_domain="workspace")
        assert len(events) == 1
        assert events[0].result == "discarded"
        assert events[0].commit_revision is None
    finally:
        store.close()


def test_workspace_config_event_relay_is_independent_per_consumer(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
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


def test_workspace_config_apply_claim_uses_lease_and_fencing(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        store.ensure_active_config_snapshot(
            config_domain="workspace",
            payload={"config_version": 1},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active",
            secret_bindings={},
            schema_version=1,
        )
        store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-1",
            idempotency_key="reload-1",
            payload={"config_version": 1},
            source_baseline={},
            candidate_digest="candidate",
            effective_digest="candidate",
            target_generation="workspace-runtime",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=1,
        )
        claim = store.acquire_config_apply_claim(
            config_domain="workspace",
            candidate_id="candidate-1",
            attempt_id="attempt-1",
            apply_id="apply-1",
            owner="test",
            base_active_revision=1,
            target_generation="workspace-runtime",
            lease_seconds=30,
        )
        assert claim.fencing_token
        with pytest.raises(ConfigConflictError):
            store.acquire_config_apply_claim(
                config_domain="workspace",
                candidate_id="candidate-2",
                attempt_id="attempt-2",
                apply_id="apply-2",
                owner="other",
                base_active_revision=1,
                target_generation="workspace-runtime",
                lease_seconds=30,
            )
        renewed = store.renew_config_apply_claim(
            config_domain="workspace",
            apply_id="apply-1",
            fencing_token=claim.fencing_token,
            lease_seconds=30,
        )
        assert renewed.apply_id == claim.apply_id
        with pytest.raises(ConfigConflictError):
            store.release_config_apply_claim(
                config_domain="workspace",
                apply_id="apply-1",
                fencing_token="stale-token",
            )
        store.release_config_apply_claim(
            config_domain="workspace",
            apply_id="apply-1",
            fencing_token=claim.fencing_token,
        )
        assert store.get_config_apply_claim(config_domain="workspace") is None
    finally:
        store.close()


def test_expired_workspace_apply_recovers_candidate_journal_and_claim(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path)
    try:
        store.ensure_active_config_snapshot(
            config_domain="workspace",
            payload={"config_version": 1},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            secret_bindings={},
            schema_version=1,
            promoted_generation="workspace-old",
        )
        pending = store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-expired",
            idempotency_key="reload:expired",
            payload={"config_version": 1},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="candidate-digest",
            target_generation="workspace-new",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=1,
            source_generation=0,
        )
        claim = store.begin_config_apply(
            config_domain="workspace",
            candidate_id=pending.candidate_id,
            attempt_id="attempt-expired",
            apply_id="apply-expired",
            owner="test",
            base_active_revision=1,
            target_generation="workspace-new",
            pending_revision=pending.pending_revision,
            source_baseline={},
            active_baseline={},
            lease_seconds=0.01,
        )
        connection = store.connection()
        try:
            connection.execute(
                "UPDATE config_apply_claim SET lease_expires_at = ?",
                (datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),),
            )
            connection.commit()
        finally:
            connection.close()
        assert store.recover_expired_config_applies(config_domain="workspace") == (
            pending.candidate_id,
        )
        recovered = store.get_pending_config_candidate(
            config_domain="workspace", candidate_id=pending.candidate_id
        )
        assert recovered is not None
        assert recovered.state == "recovery_required"
        journal = store.get_config_apply_journal(apply_id=claim.apply_id)
        assert journal is not None
        assert journal.state == "recovery_required"
        assert store.get_config_apply_claim(config_domain="workspace") is None
    finally:
        store.close()


def test_workspace_pending_retry_reuses_candidate_and_rotates_fencing(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path)
    try:
        store.ensure_active_config_snapshot(
            config_domain="workspace",
            payload={"config_version": 1},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            secret_bindings={},
            schema_version=1,
        )
        pending = store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-retry",
            idempotency_key="reload:retry",
            payload={"config_version": 1},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="candidate-digest",
            target_generation="workspace-old",
            fencing_token="fence-old",
            state="candidate_validated",
            base_active_revision=1,
            source_generation=0,
            candidate_ref="candidate-ref-retry",
        )
        claim = store.begin_config_apply(
            config_domain="workspace",
            candidate_id=pending.candidate_id,
            attempt_id="attempt-retry",
            apply_id="apply-retry",
            owner="test",
            base_active_revision=1,
            target_generation="workspace-old",
            pending_revision=pending.pending_revision,
            source_baseline={},
            active_baseline={},
        )
        connection = store.connection()
        try:
            connection.execute(
                "UPDATE config_apply_claim SET lease_expires_at = ?",
                (datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(),),
            )
            connection.commit()
        finally:
            connection.close()
        assert store.recover_expired_config_applies(config_domain="workspace") == (
            pending.candidate_id,
        )
        retried = store.retry_pending_config_restart(
            candidate_ref="candidate-ref-retry",
            target_generation="workspace-new",
        )
        assert retried.candidate_id == pending.candidate_id
        assert retried.idempotency_key == pending.idempotency_key
        assert retried.state == "pending_restart"
        assert retried.target_generation == "workspace-new"
        assert retried.fencing_token not in {None, "fence-old"}
        assert store.get_config_apply_journal(apply_id=claim.apply_id).state == (
            "recovery_required"
        )
    finally:
        store.close()


def test_corrupt_workspace_active_snapshot_enters_explicit_recovery_state(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        store.ensure_active_config_snapshot(
            config_domain="workspace",
            payload={"config_version": 1},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            secret_bindings={},
            schema_version=1,
        )
        connection = store.connection()
        try:
            connection.execute(
                "UPDATE config_active_snapshot SET payload_json = ? WHERE config_domain = 'workspace'",
                ("not-json",),
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(ValueError, match="active snapshot payload"):
            store.get_active_config_snapshot("workspace")
        connection = store.connection()
        try:
            row = connection.execute(
                "SELECT state, last_error FROM config_active_snapshot WHERE config_domain = 'workspace'"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        assert row[0] == "recovery_required"
        assert "损坏" in row[1]
    finally:
        store.close()


def test_legacy_workspace_secret_migration_blocks_literal_and_normalizes_env(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        connection = store.connection()
        try:
            connection.execute(
                """
                INSERT INTO workspace_config(config_key, config_version, payload_json, updated_at)
                VALUES ('legacy-literal', 1, ?, '2026-01-01T00:00:00+00:00')
                """,
                (json.dumps({"api_key": "literal-secret"}),),
            )
            connection.execute(
                """
                INSERT INTO workspace_config(config_key, config_version, payload_json, updated_at)
                VALUES ('legacy-env', 1, ?, '2026-01-01T00:00:00+00:00')
                """,
                (json.dumps({"api_key": "${ROTATED_API_KEY}"}),),
            )
            connection.commit()
        finally:
            connection.close()

        blocked = store.migrate_legacy_config_secrets("legacy-literal")
        assert blocked == ("/api_key",)
        literal_record = store.get_config("legacy-literal")
        assert literal_record is not None
        assert literal_record.payload["api_key"].startswith("literal-sha256:")
        assert "literal-secret" not in json.dumps(literal_record.payload)

        assert store.migrate_legacy_config_secrets("legacy-env") == ()
        env_record = store.get_config("legacy-env")
        assert env_record is not None
        assert env_record.payload == {"api_key": "env:ROTATED_API_KEY"}
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


def test_source_journal_distinguishes_a_b_a_and_deduplicates_a_a(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        first = store.append_config_source_journal(
            source_key="user",
            source_event_id="source-event-1",
            source_path=tmp_path / "workspace.jsonc",
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
            source_path=tmp_path / "workspace.jsonc",
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
            source_path=tmp_path / "workspace.jsonc",
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
            source_path=tmp_path / "workspace.jsonc",
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


def test_source_journal_replays_stopped_workspace_and_reports_partial_fanout(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        first = store.append_config_source_journal(
            source_key="shared-user-workspace",
            source_event_id="shared-1",
            source_path=tmp_path / "workspace.jsonc",
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
            source_path=tmp_path / "workspace.jsonc",
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


def test_workspace_config_event_cursor_reports_snapshot_required(tmp_path):
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
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
