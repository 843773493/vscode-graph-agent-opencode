from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def test_workspace_apply_journal_records_idempotent_side_effect(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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

def test_workspace_apply_journal_requires_explicit_compensation_result(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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

def test_workspace_active_promotion_commits_apply_journal_atomically(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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
    workspace_root,
):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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

def test_workspace_pending_candidate_can_be_explicitly_discarded(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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

def test_workspace_config_apply_claim_uses_lease_and_fencing(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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

def test_expired_workspace_apply_recovers_candidate_journal_and_claim(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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
                (datetime(2000, 1, 1, tzinfo=UTC).isoformat(),),
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

def test_workspace_pending_retry_reuses_candidate_and_rotates_fencing(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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
                (datetime(2000, 1, 1, tzinfo=UTC).isoformat(),),
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

def test_corrupt_workspace_active_snapshot_enters_explicit_recovery_state(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
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
