from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
)


def test_gateway_state_shared_processes_must_be_explicit(tmp_path):
    first = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        with pytest.raises(RuntimeError, match="已被另一个进程占用"):
            GatewayStateStore(path=tmp_path / "gateway.sqlite")
        shared = GatewayStateStore(
            path=tmp_path / "gateway.sqlite",
            allow_shared_processes=True,
        )
        shared.close()
    finally:
        first.close()


def test_gateway_state_keeps_config_in_control_database(tmp_path):
    store = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        store.set_config(
            config_key="gateway",
            config_version=3,
            payload={"history_loading": {"initial_turns": 1}},
        )
        record = store.get_config("gateway")
        assert record is not None
        assert record.config_version == 3
        assert record.payload == {"history_loading": {"initial_turns": 1}}
        assert store.diagnostics().path.endswith("gateway.sqlite")
    finally:
        store.close()


def test_gateway_source_layer_cas_and_owner_generation_guard(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        source_path = tmp_path / "workspace.jsonc"
        state.sync_config_source(
            config_key="workspace_mutable_override",
            source_path=source_path,
            config_version=1,
            presence="present",
            payload={"ui": {"a": 1}},
            layer_digest="digest-1",
        )
        # layer revision / digest 显式 CAS 必须拒绝过期输入
        for key, kwargs in (
            ("revision", {"expected_layer_revision": 99}),
            ("digest", {"expected_layer_revision": 1, "expected_layer_digest": "stale"}),
        ):
            with pytest.raises(ConfigConflictError, match="source layer CAS"):
                state.sync_config_source(
                    config_key="workspace_mutable_override",
                    source_path=source_path,
                    config_version=2,
                    presence="present",
                    payload={"ui": {"a": 2}},
                    layer_digest="digest-2",
                    **kwargs,
                )
        # 首次写入带期望 revision 时必须报初始 CAS 冲突
        with pytest.raises(ConfigConflictError, match="初始 CAS"):
            state.sync_config_source(
                config_key="brand_new_key",
                source_path=source_path,
                config_version=1,
                presence="present",
                payload={"ui": {}},
                layer_digest="digest-new",
                expected_layer_revision=1,
            )
    finally:
        state.close()


def test_gateway_source_journal_owner_generation_guard_is_enforced(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        source_path = tmp_path / "workspace.jsonc"
        state.append_config_source_journal(
            source_key="user",
            source_event_id="event-1",
            source_path=source_path,
            presence="present",
            layer_revision=1,
            layer_digest="a",
            previous_digest=None,
            origin="file-watcher",
            fanout_id="fanout-1",
        )
        # 人为把 owner 水位推到与 journal 水位不一致：下一次 append 必须 fail closed
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE config_source_owner SET next_generation = ? WHERE source_key = 'user'",
                (99,),
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ConfigConflictError, match="source owner generation CAS"):
            state.append_config_source_journal(
                source_key="user",
                source_event_id="event-2",
                source_path=source_path,
                presence="absent",
                layer_revision=2,
                layer_digest="b",
                previous_digest="a",
                origin="file-watcher",
                fanout_id="fanout-2",
            )
    finally:
        state.close()


def test_gateway_source_owner_guard_applies_inside_sync_transaction(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        source_path = tmp_path / "workspace.jsonc"
        # sync_config_source 在同一事务内经 _append_config_source_journal_in_connection
        # 追加 journal，owner 水位不一致时必须在事务内 fail closed 并回滚整个 sync。
        state.sync_config_source(
            config_key="workspace_mutable_override",
            source_path=source_path,
            config_version=1,
            presence="present",
            payload={"ui": {"a": 1}},
            layer_digest="d1",
            journal_origin="file-watcher",
            source_event_id="sync-event-1",
            fanout_id="sync-fanout-1",
        )
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE config_source_owner SET next_generation = 99"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ConfigConflictError, match="source owner generation CAS"):
            state.sync_config_source(
                config_key="workspace_mutable_override",
                source_path=tmp_path / "next.jsonc",
                config_version=2,
                presence="present",
                payload={"ui": {"a": 2}},
                layer_digest="d2",
                journal_origin="file-watcher",
                source_event_id="sync-event-2",
                fanout_id="sync-fanout-2",
            )
        # 事务整体回滚：layer 仍停留在上一次成功提交的 revision
        assert state.get_source_layer("workspace_mutable_override").layer_revision == 1
    finally:
        state.close()


def test_gateway_registry_uses_sqlite_without_mixing_session_indexes(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        registry = GatewayWorkspaceRegistry(
            storage_path=tmp_path / "workspaces.json",
            state_store=state,
        )
        registry.upsert(
            WorkspaceTarget(
                workspace_id="workspace-a",
                name="A",
                root_path=str(tmp_path / "workspace-a"),
                backend_url="http://127.0.0.1:8010",
                connection_kind="local",
            )
        )
        restored = GatewayWorkspaceRegistry(
            storage_path=tmp_path / "workspaces.json",
            state_store=state,
        )
        assert [target.workspace_id for target in restored.targets()] == ["workspace-a"]
        assert state.diagnostics().path.endswith("gateway.sqlite")
        assert not (tmp_path / "workspace-a" / "rollout" / "index.sqlite").exists()
    finally:
        state.close()


def test_gateway_restart_intent_requires_matching_candidate_and_proof(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate_1_hash",
            idempotency_key="reload:1:hash",
            payload={"workspaces": []},
            source_baseline={},
            candidate_digest="hash",
            effective_digest="hash",
            target_generation="gateway-runtime",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=7,
        )
        state.update_pending_config_candidate_state(
            config_domain="gateway",
            candidate_id="candidate_1_hash",
            expected_state="candidate_validated",
            state="pending_restart",
        )
        intent = state.request_gateway_restart(
            candidate_ref="candidate-ref-1",
            candidate_id="candidate_1_hash",
            base_active_revision=1,
            old_generation="gateway-generation-old",
            target_generation="gateway-generation-new",
            requested_by="test",
            gateway_id="gateway-test",
        )
        assert intent.state == "pending"
        assert intent.gateway_id == "gateway-test"
        assert state.load_gateway_pending_candidate(
            candidate_ref="candidate-ref-1",
            gateway_id="gateway-test",
        ).candidate_id == "candidate_1_hash"
        with pytest.raises(ConfigConflictError, match="不属于当前 Gateway"):
            state.load_gateway_pending_candidate(
                candidate_ref="candidate-ref-1",
                gateway_id="another-gateway",
            )
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE gateway_restart_intent SET gateway_id = NULL "
                "WHERE candidate_ref = ?",
                ("candidate-ref-1",),
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ConfigConflictError, match="缺少 gateway_id"):
            state.load_gateway_pending_candidate(
                candidate_ref="candidate-ref-1",
                gateway_id="gateway-test",
            )
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE gateway_restart_intent SET gateway_id = ?, expires_at = ? "
                "WHERE candidate_ref = ?",
                (
                    "gateway-test",
                    "1970-01-01T00:00:00+00:00",
                    "candidate-ref-1",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(ConfigConflictError, match="已过期"):
            state.load_gateway_pending_candidate(
                candidate_ref="candidate-ref-1",
                gateway_id="gateway-test",
            )
        pending = state.get_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate_1_hash",
        )
        assert pending is not None
        assert pending.base_active_revision == 7
        assert pending.persistence_location == str(state.path)
        updated = state.update_gateway_restart_intent(
            candidate_ref="candidate-ref-1",
            expected_state="pending",
            state="applying",
            fencing_token=intent.fencing_token,
        )
        assert updated.state == "applying"
        proof = state.update_gateway_restart_intent(
            candidate_ref="candidate-ref-1",
            expected_state="applying",
            state="active",
            fencing_token=intent.fencing_token,
            health_proof={
                "generation": "gateway-generation-new",
                "health_digest": "health-hash",
            },
        )
        assert proof.health_proof == {
            "generation": "gateway-generation-new",
            "health_digest": "health-hash",
        }
    finally:
        state.close()


def test_gateway_apply_journal_records_idempotent_side_effect(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.start_config_apply_journal(
            config_domain="gateway",
            apply_id="apply-side-effect",
            candidate_id="candidate-side-effect",
            attempt_id="attempt-side-effect",
            owner="test",
            base_active_revision=1,
            pending_revision=2,
            source_baseline={},
            active_baseline={},
        )
        side_effect = {"resource": "registry", "action": "reconcile"}
        first = state.append_config_apply_side_effect(
            apply_id="apply-side-effect",
            side_effect=side_effect,
        )
        second = state.append_config_apply_side_effect(
            apply_id="apply-side-effect",
            side_effect=side_effect,
        )
        assert first.side_effects == (side_effect,)
        assert second.side_effects == (side_effect,)
        state.update_config_apply_journal(
            apply_id="apply-side-effect",
            expected_state="applying",
            state="committed",
        )
        with pytest.raises(RuntimeError, match="副作用追加状态 CAS"):
            state.append_config_apply_side_effect(
                apply_id="apply-side-effect",
                side_effect={"resource": "proxy", "action": "drain"},
            )
    finally:
        state.close()


def test_gateway_apply_journal_requires_explicit_compensation_result(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.start_config_apply_journal(
            config_domain="gateway",
            apply_id="apply-compensation",
            candidate_id="candidate-compensation",
            attempt_id="attempt-compensation",
            owner="test",
            base_active_revision=1,
            pending_revision=2,
            source_baseline={},
            active_baseline={},
        )
        state.append_config_apply_side_effect(
            apply_id="apply-compensation",
            side_effect={"resource": "registry", "action": "reconcile"},
        )
        failed = state.record_config_apply_compensation(
            apply_id="apply-compensation",
            expected_state="applying",
            compensation={
                "resource": "registry",
                "action": "rollback",
                "status": "failed",
                "error": "旧 registry 无法恢复",
            },
        )
        assert failed.state == "recovery_required"
        assert failed.last_error == "旧 registry 无法恢复"

        compensated = state.record_config_apply_compensation(
            apply_id="apply-compensation",
            compensation={
                "resource": "registry",
                "action": "rollback",
                "status": "succeeded",
            },
        )
        assert compensated.state == "compensated"
        assert [item["phase"] for item in compensated.side_effects if "phase" in item] == [
            "compensation",
            "compensation",
        ]
    finally:
        state.close()


def test_gateway_active_promotion_commits_apply_journal_atomically(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        active = state.ensure_active_config_snapshot(
            config_domain="gateway",
            payload={"config_version": 1, "workspaces": []},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            schema_version=2,
            secret_bindings={},
        )
        pending = state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate-atomic-promotion",
            idempotency_key="reload:atomic-promotion",
            payload={"config_version": 2, "workspaces": []},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="candidate-digest",
            target_generation="gateway-runtime-2",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=active.active_revision,
            source_generation=0,
        )
        claim = state.begin_config_apply(
            config_domain="gateway",
            candidate_id=pending.candidate_id,
            attempt_id="attempt-atomic-promotion",
            apply_id="apply-atomic-promotion",
            owner="gateway-config-service",
            base_active_revision=active.active_revision,
            target_generation="gateway-runtime-2",
            pending_revision=pending.pending_revision,
            source_baseline={},
            active_baseline={},
        )
        state.assert_config_apply_claim(
            config_domain="gateway",
            apply_id=claim.apply_id,
            fencing_token=claim.fencing_token,
        )
        with pytest.raises(ConfigConflictError, match="fencing"):
            state.assert_config_apply_claim(
                config_domain="gateway",
                apply_id=claim.apply_id,
                fencing_token="stale-fence",
            )

        promoted = state.promote_active_config_snapshot(
            config_domain="gateway",
            candidate_id=pending.candidate_id,
            payload={"config_version": 2, "workspaces": []},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="candidate-digest",
            schema_version=2,
            expected_active_revision=active.active_revision,
            expected_pending_revision=pending.pending_revision,
            expected_pending_state="applying",
            expected_source_baseline={},
            expected_source_generation=0,
            expected_layer_revisions={},
            expected_layer_digests={},
            expected_fencing_token=claim.fencing_token,
            promoted_apply_id=claim.apply_id,
        )

        journal = state.get_config_apply_journal(apply_id=claim.apply_id)
        assert journal is not None
        assert journal.state == "committed"
        assert promoted.effective_digest == "candidate-digest"
        assert state.get_pending_config_candidate(
            config_domain="gateway", candidate_id=pending.candidate_id
        ).state == "active"
    finally:
        state.close()


def test_gateway_pending_candidate_persists_secret_binding_without_secret_value(
    tmp_path,
):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        pending = state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate-secret-binding",
            idempotency_key="reload:secret-binding",
            payload={"config_version": 1, "workspaces": [], "token": "env:BOXTEAM_TOKEN"},
            source_baseline={},
            candidate_digest="candidate-secret-digest",
            effective_digest="candidate-secret-digest",
            target_generation="gateway-runtime-secret",
            fencing_token=None,
            state="candidate_validated",
            source_generation=1,
        )

        binding = pending.secret_bindings["/token"]
        assert binding["secret_ref"] == "env:BOXTEAM_TOKEN"
        assert binding["secret_version"]
        assert binding["binding_digest"]
        with state.connection() as connection:
            persisted = connection.execute(
                "SELECT payload_json, secret_bindings_json FROM config_pending_candidate"
            ).fetchone()
        assert "BOXTEAM_TOKEN" in persisted[0]
        assert "BOXTEAM_TOKEN" in persisted[1]
    finally:
        state.close()


def test_gateway_restart_recovery_rotates_generation_and_fencing_token(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate-recovery",
            idempotency_key="reload:recovery",
            payload={"workspaces": []},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="effective-digest",
            target_generation="gateway-generation-old",
            fencing_token=None,
            state="candidate_validated",
        )
        state.update_pending_config_candidate_state(
            config_domain="gateway",
            candidate_id="candidate-recovery",
            expected_state="candidate_validated",
            state="pending_restart",
        )
        intent = state.request_gateway_restart(
            candidate_ref="candidate-ref-recovery",
            candidate_id="candidate-recovery",
            base_active_revision=None,
            old_generation="gateway-generation-before",
            target_generation="gateway-generation-old",
            requested_by="test",
        )
        claim = state.acquire_config_apply_claim(
            config_domain="gateway",
            candidate_id="candidate-recovery",
            attempt_id="attempt-recovery",
            apply_id="apply-recovery",
            owner="gateway-restart-supervisor",
            base_active_revision=None,
            target_generation=intent.target_generation,
            fencing_token=intent.fencing_token,
        )
        state.update_gateway_restart_intent(
            candidate_ref=intent.candidate_ref,
            expected_state="pending",
            state="applying",
            fencing_token=claim.fencing_token,
        )
        state.update_pending_config_candidate_state(
            config_domain="gateway",
            candidate_id="candidate-recovery",
            expected_state="pending_restart",
            state="applying",
        )
        state.start_config_apply_journal(
            config_domain="gateway",
            apply_id=claim.apply_id,
            candidate_id="candidate-recovery",
            attempt_id=claim.attempt_id,
            owner=claim.owner,
            base_active_revision=None,
            pending_revision=1,
            source_baseline={},
            active_baseline={},
        )
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE config_apply_claim SET lease_expires_at = ? WHERE config_domain = 'gateway'",
                ("1970-01-01T00:00:00+00:00",),
            )
            connection.commit()
        finally:
            connection.close()

        assert state.recover_expired_config_applies(config_domain="gateway") == (
            "candidate-recovery",
        )
        assert state.get_pending_config_candidate(
            config_domain="gateway", candidate_id="candidate-recovery"
        ).state == "recovery_required"
        assert state.get_gateway_restart_intent(
            candidate_ref=intent.candidate_ref
        ).state == "recovery_required"
        assert state.get_config_apply_journal(
            apply_id=claim.apply_id
        ).state == "recovery_required"

        retried = state.retry_gateway_restart(
            candidate_ref=intent.candidate_ref,
            target_generation="gateway-generation-retry",
            requested_by="test-retry",
        )
        assert retried.state == "pending"
        assert retried.target_generation == "gateway-generation-retry"
        assert retried.fencing_token != intent.fencing_token
        pending = state.get_pending_config_candidate(
            config_domain="gateway", candidate_id="candidate-recovery"
        )
        assert pending is not None
        assert pending.state == "pending_restart"
        assert pending.target_generation == "gateway-generation-retry"
        assert pending.fencing_token == retried.fencing_token
    finally:
        state.close()


def test_expired_gateway_pending_restart_can_be_explicitly_retried(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate-expired",
            idempotency_key="reload:expired",
            payload={"workspaces": []},
            source_baseline={},
            candidate_digest="candidate-expired-digest",
            effective_digest="effective-expired-digest",
            target_generation="gateway-generation-old",
            fencing_token=None,
            state="candidate_validated",
        )
        state.update_pending_config_candidate_state(
            config_domain="gateway",
            candidate_id="candidate-expired",
            expected_state="candidate_validated",
            state="pending_restart",
        )
        intent = state.request_gateway_restart(
            candidate_ref="candidate-ref-expired",
            candidate_id="candidate-expired",
            base_active_revision=None,
            old_generation="gateway-generation-before",
            target_generation="gateway-generation-old",
            requested_by="test",
            gateway_id="gateway-test",
        )
        with state.connection() as connection:
            connection.execute(
                "UPDATE gateway_restart_intent SET expires_at = ? "
                "WHERE candidate_ref = ?",
                ("1970-01-01T00:00:00+00:00", intent.candidate_ref),
            )
            connection.commit()

        retried = state.retry_gateway_restart(
            candidate_ref=intent.candidate_ref,
            target_generation="gateway-generation-retry",
            requested_by="test-retry",
        )

        assert retried.state == "pending"
        assert retried.target_generation == "gateway-generation-retry"
        assert retried.fencing_token != intent.fencing_token
        assert retried.expires_at is not None
        assert retried.expires_at > datetime.now(timezone.utc)
    finally:
        state.close()


def test_corrupt_gateway_active_snapshot_enters_explicit_recovery_state(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.ensure_active_config_snapshot(
            config_domain="gateway",
            payload={"config_version": 1, "workspaces": []},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            secret_bindings={},
            schema_version=2,
        )
        connection = state.connection()
        try:
            connection.execute(
                "UPDATE config_active_snapshot SET payload_json = ? WHERE config_domain = 'gateway'",
                ("not-json",),
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(ValueError, match="Gateway active payload"):
            state.get_active_config_snapshot("gateway")
        connection = state.connection()
        try:
            row = connection.execute(
                "SELECT state, last_error FROM config_active_snapshot WHERE config_domain = 'gateway'"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        assert row[0] == "recovery_required"
        assert "损坏" in row[1]
    finally:
        state.close()


def test_gateway_config_storage_persists_literal_secret_fields(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.set_config(
            config_key="gateway-secret",
            config_version=1,
            payload={"api_key": "local-dummy-key"},
        )
        record = state.get_config("gateway-secret")
        assert record is not None
        assert record.payload["api_key"] == "local-dummy-key"
    finally:
        state.close()


def test_legacy_gateway_secret_migration_blocks_irreversible_digest(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        connection = state.connection()
        try:
            connection.execute(
                """
                INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                VALUES ('legacy-digest', 1, ?, '2026-01-01T00:00:00+00:00')
                """,
                (json.dumps({"api_key": "literal-sha256:deadbeef"}),),
            )
            connection.commit()
        finally:
            connection.close()

        assert state.migrate_legacy_config_secrets("legacy-digest") == ("/api_key",)
        digest_record = state.get_config("legacy-digest")
        assert digest_record is not None
        assert digest_record.payload["api_key"] == "literal-sha256:deadbeef"
    finally:
        state.close()


def test_gateway_config_event_outbox_claim_retry_and_dedup(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        created = state.append_config_event(
            ConfigEventInput(
                event_id="gateway-outbox-1",
                config_domain="gateway",
                candidate_id="candidate-1",
                attempt_id="attempt-1",
                apply_id="apply-1",
                idempotency_key="reload-outbox-1",
                commit_revision=None,
                active_revision=1,
                pending_revision=2,
                source="watcher",
                result="restart_required",
                activation_scope="restart_gateway",
            )
        )
        assert created.relay_state == "pending"
        claimed = state.claim_config_event_relay(
            event_id=created.event_id,
            consumer_id="sse-relay",
        )
        assert claimed is not None
        assert state.fail_config_event_relay(
            event_id=created.event_id,
            consumer_id="sse-relay",
            error="client disconnected",
        ).relay_state == "failed"
        retried = state.claim_config_event_relay(
            event_id=created.event_id,
            consumer_id="sse-relay",
        )
        assert retried is not None
        assert retried.event_seq == created.event_seq
        assert retried.relay_attempts == 2
        assert state.mark_config_event_relay_delivered(
            event_id=created.event_id,
            consumer_id="sse-relay",
        ).relay_state == "delivered"
        assert state.list_config_events_for_relay(config_domain="gateway") == ()
    finally:
        state.close()


def test_gateway_outbox_relay_rejects_hijack_and_stale_finalize(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        event = state.append_config_event(
            ConfigEventInput(
                event_id="gateway-outbox-hijack",
                config_domain="gateway",
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
        )
        claimed = state.claim_config_event_relay(
            event_id=event.event_id,
            consumer_id="relay-a",
        )
        assert claimed is not None
        # 已由 relay-a claim 的事件不能被 relay-b 确认或标记失败
        with pytest.raises(ConfigConflictError, match="不属于当前 consumer"):
            state.mark_config_event_relay_delivered(
                event_id=event.event_id,
                consumer_id="relay-b",
            )
        with pytest.raises(ConfigConflictError, match="不属于当前 consumer"):
            state.fail_config_event_relay(
                event_id=event.event_id,
                consumer_id="relay-b",
                error="hijack",
            )
        # relay-a 的确认是幂等的，重复确认不改变归属
        delivered = state.mark_config_event_relay_delivered(
            event_id=event.event_id,
            consumer_id="relay-a",
        )
        assert delivered.relay_state == "delivered"
        assert state.mark_config_event_relay_delivered(
            event_id=event.event_id,
            consumer_id="relay-a",
        ).relay_state == "delivered"
        # 已 delivered 是全局幂等终态：第三方确认同样返回 delivered 而不报错
        assert state.mark_config_event_relay_delivered(
            event_id=event.event_id,
            consumer_id="relay-b",
        ).relay_state == "delivered"
    finally:
        state.close()


def test_gateway_config_event_relay_is_independent_per_consumer(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        event = state.append_config_event(
            ConfigEventInput(
                event_id="gateway-consumer-independent",
                config_domain="gateway",
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
        )
        first = state.claim_config_events_for_consumer(
            config_domain="gateway",
            after=0,
            consumer_id="consumer-a",
        )
        second = state.claim_config_events_for_consumer(
            config_domain="gateway",
            after=0,
            consumer_id="consumer-b",
        )
        assert [item.event_id for item in first] == [event.event_id]
        assert [item.event_id for item in second] == [event.event_id]
        state.mark_config_event_delivered_for_consumer(
            event_id=event.event_id,
            consumer_id="consumer-a",
        )
        assert state.claim_config_events_for_consumer(
            config_domain="gateway",
            after=0,
            consumer_id="consumer-a",
        ) == ()
    finally:
        state.close()


def test_gateway_registry_apply_journal_commits_with_registry_revision(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        revision = state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "workspace-a",
                "targets": [
                    {
                        "workspace_id": "workspace-a",
                        "owner": "manual",
                        "target_namespace": "gateway",
                    }
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=0,
            owner="manual_crud",
        )
        assert revision == 1
        journal = state.list_registry_apply_journal()
        assert len(journal) == 1
        assert journal[0]["owner"] == "manual_crud"
        assert journal[0]["state"] == "committed"
        assert journal[0]["target_revision"] == 1
    finally:
        state.close()


def test_gateway_pending_registry_baseline_rebases_only_startup_owned_changes(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.ensure_active_config_snapshot(
            config_domain="gateway",
            payload={"config_version": 1},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            schema_version=2,
        )
        pending = state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate-startup-registry-rebase",
            idempotency_key="reload:startup-registry-rebase",
            payload={"config_version": 2},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="candidate-digest",
            target_generation="gateway-runtime",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=1,
        )
        claim = state.begin_config_apply(
            config_domain="gateway",
            candidate_id=pending.candidate_id,
            attempt_id="attempt-startup-registry-rebase",
            apply_id="apply-startup-registry-rebase",
            owner="gateway-restart-supervisor",
            base_active_revision=1,
            target_generation="gateway-runtime",
            pending_revision=pending.pending_revision,
            source_baseline={},
            active_baseline={},
            registry_revision=0,
        )
        state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "system-default",
                "targets": [
                    {
                        "workspace_id": "system-default",
                        "owner": "system",
                        "target_namespace": "gateway",
                    }
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=0,
            owner="system",
        )

        assert state.rebase_config_apply_registry_revision(
            apply_id=claim.apply_id,
            expected_registry_revision=0,
        ) == 1
        journal = state.get_config_apply_journal(apply_id=claim.apply_id)
        assert journal is not None
        assert journal.registry_revision == 1

        state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "system-default",
                "targets": [
                    {
                        "workspace_id": "system-default",
                        "owner": "system",
                        "target_namespace": "gateway",
                    },
                    {
                        "workspace_id": "manual-target",
                        "owner": "manual",
                        "target_namespace": "gateway-manual",
                    },
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=1,
            owner="manual_crud",
        )
        with pytest.raises(ConfigConflictError, match="非启动 registry 修改"):
            state.rebase_config_apply_registry_revision(
                apply_id=claim.apply_id,
                expected_registry_revision=1,
            )
    finally:
        state.close()


def test_gateway_system_registry_update_preserves_manual_target_owner(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial_payload = {
            "schema_version": 10,
            "active_workspace_id": "manual-target",
            "targets": [
                {
                    "workspace_id": "manual-target",
                    "owner": "manual",
                    "target_namespace": "gateway-manual",
                    "backend_url": "http://127.0.0.1:18010",
                    "connection_error": None,
                }
            ],
            "remote_gateway_connections": [],
        }
        state.replace_workspace_registry(
            initial_payload,
            expected_revision=0,
            owner="manual_crud",
        )

        updated_payload = {
            **initial_payload,
            "targets": [
                {
                    **initial_payload["targets"][0],
                    "backend_url": "http://127.0.0.1:18011",
                    "connection_error": "Gateway 正在恢复工作区运行时",
                }
            ],
        }
        assert state.replace_workspace_registry(
            updated_payload,
            expected_revision=1,
            owner="system",
        ) == 2

        restored = state.load_workspace_registry()
        assert restored is not None
        assert restored["targets"] == updated_payload["targets"]

        changed_identity_payload = {
            **updated_payload,
            "targets": [
                {
                    **updated_payload["targets"][0],
                    "target_namespace": "gateway-other",
                }
            ],
        }
        with pytest.raises(PermissionError, match="其他 target owner"):
            state.replace_workspace_registry(
                changed_identity_payload,
                expected_revision=2,
                owner="system",
            )
    finally:
        state.close()


def test_gateway_registry_normalizes_legacy_null_owner(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        connection = state.connection()
        try:
            connection.execute(
                """
                INSERT INTO gateway_workspace_registry(
                    workspace_id, position, active, payload_json, updated_at
                ) VALUES ('legacy-system', 0, 1, ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "workspace_id": "legacy-system",
                            "owner": None,
                            "target_namespace": "gateway",
                        },
                        ensure_ascii=False,
                    ),
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        revision = state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "legacy-system",
                "targets": [
                    {
                        "workspace_id": "legacy-system",
                        "owner": "system",
                        "target_namespace": "gateway",
                    }
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=0,
            owner="system",
        )

        assert revision == 1
        restored = state.load_workspace_registry()
        assert restored is not None
        assert restored["targets"] == [
            {
                "workspace_id": "legacy-system",
                "owner": "system",
                "target_namespace": "gateway",
            }
        ]
    finally:
        state.close()


def test_gateway_registry_apply_journal_recovery_is_explicit(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        connection = state.connection()
        try:
            connection.execute(
                """
                INSERT INTO registry_apply_journal(
                    apply_id, owner, base_revision, state, payload_digest,
                    created_at, updated_at
                ) VALUES ('apply-orphan', 'config_batch', 3, 'applying', 'digest',
                          '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
                """
            )
        finally:
            connection.close()

        recovered = state.recover_registry_apply_journal()
        assert recovered[0]["apply_id"] == "apply-orphan"
        assert recovered[0]["state"] == "recovery_required"
        assert "提交前退出" in str(recovered[0]["last_error"])
    finally:
        state.close()


def test_gateway_registry_rejects_non_list_targets_and_connections(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        with pytest.raises(ValueError, match="payload 结构无效"):
            state.replace_workspace_registry(
                {
                    "schema_version": 10,
                    "targets": "not-a-list",
                    "remote_gateway_connections": [],
                },
                expected_revision=0,
                owner="manual_crud",
            )
        with pytest.raises(ValueError, match="payload 结构无效"):
            state.replace_workspace_registry(
                {
                    "schema_version": 10,
                    "targets": [],
                    "remote_gateway_connections": "not-a-list",
                },
                expected_revision=0,
                owner="manual_crud",
            )
    finally:
        state.close()


def test_gateway_registry_allows_multiple_remote_workspaces_per_connection(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "remote-a",
                "targets": [
                    {
                        "workspace_id": "remote-a",
                        "owner": "remote_projection",
                        "target_namespace": "remote:connection-1",
                        "connection_id": "connection-1",
                        "remote_workspace_id": "remote-a",
                    },
                    {
                        "workspace_id": "remote-b",
                        "owner": "remote_projection",
                        "target_namespace": "remote:connection-1",
                        "connection_id": "connection-1",
                        "remote_workspace_id": "remote-b",
                    },
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=0,
        )
        assert len(state.load_workspace_registry()["targets"]) == 2
    finally:
        state.close()


def test_gateway_registry_start_journal_rejects_stale_expected_revision(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        assert (
            state.replace_workspace_registry(
                {
                    "schema_version": 10,
                    "targets": [],
                    "remote_gateway_connections": [],
                },
                expected_revision=0,
                owner="manual_crud",
            )
            == 1
        )
        with pytest.raises(ConfigConflictError, match="revision CAS 冲突"):
            state.replace_workspace_registry(
                {
                    "schema_version": 10,
                    "targets": [],
                    "remote_gateway_connections": [],
                },
                expected_revision=0,
                owner="manual_crud",
            )
        assert state.get_registry_revision() == 1
        journals = state.list_registry_apply_journal()
        assert [journal["state"] for journal in journals] == ["committed"]
    finally:
        state.close()


def test_gateway_registry_rejects_duplicate_owner_namespace_identity(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        with pytest.raises(ValueError, match="owner/namespace/identity 重复"):
            state.replace_workspace_registry(
                {
                    "schema_version": 10,
                    "active_workspace_id": "config-a",
                    "targets": [
                        {
                            "workspace_id": "config-a",
                            "owner": "config",
                            "target_namespace": "gateway-config",
                            "connection_id": "same-connection",
                        },
                        {
                            "workspace_id": "config-b",
                            "owner": "config",
                            "target_namespace": "gateway-config",
                            "connection_id": "same-connection",
                        },
                    ],
                    "remote_gateway_connections": [],
                },
                expected_revision=0,
            )
        assert state.get_registry_revision() == 0
        journals = state.list_registry_apply_journal()
        assert len(journals) == 1
        assert journals[0]["state"] == "failed"
    finally:
        state.close()


def test_gateway_registry_scoped_batch_preserves_other_owners(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "config-old",
                "targets": [
                    {
                        "workspace_id": "config-old",
                        "owner": "config",
                        "target_namespace": "gateway-config",
                        "connection_id": "connection-old",
                    },
                    {
                        "workspace_id": "manual-1",
                        "owner": "manual",
                        "target_namespace": "gateway-manual",
                    },
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=0,
            owner="registry",
        )

        revision = state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "config-new",
                "targets": [
                    {
                        "workspace_id": "config-new",
                        "owner": "config",
                        "target_namespace": "gateway-config",
                        "connection_id": "connection-new",
                    }
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=1,
            owner="config_batch",
        )

        assert revision == 2
        restored = state.load_workspace_registry()
        assert restored is not None
        assert [target["workspace_id"] for target in restored["targets"]] == [
            "config-new",
            "manual-1",
        ]
        assert restored["targets"][1]["owner"] == "manual"

        with pytest.raises(PermissionError, match="改变 target owner"):
            state.replace_workspace_registry(
                {
                    "schema_version": 10,
                    "active_workspace_id": "manual-1",
                    "targets": [
                        {
                            "workspace_id": "manual-1",
                            "owner": "config",
                            "target_namespace": "gateway-config",
                            "connection_id": "connection-manual",
                        }
                    ],
                    "remote_gateway_connections": [],
                },
                expected_revision=2,
                owner="config_batch",
            )
    finally:
        state.close()


def test_gateway_registry_manual_mutation_preserves_config_targets(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        registry = GatewayWorkspaceRegistry(
            storage_path=tmp_path / "workspaces.json",
            state_store=state,
        )
        registry.upsert(
            WorkspaceTarget(
                workspace_id="config-target",
                name="配置目标",
                root_path="/workspace/config-target",
                backend_url="http://127.0.0.1:18010",
                connection_kind="local",
                owner="config",
            )
        )
        registry.upsert(
            WorkspaceTarget(
                workspace_id="manual-target",
                name="手动目标",
                root_path="/workspace/manual-target",
                backend_url="http://127.0.0.1:18011",
                connection_kind="local",
                owner="manual",
            )
        )

        registry.rename("manual-target", "手动目标（已改名）")
        registry.activate("config-target")
        registry.reorder(["manual-target", "config-target"])

        assert registry.resolve("config-target").owner == "config"
        assert registry.resolve("manual-target").name == "手动目标（已改名）"
        with pytest.raises(PermissionError, match="不能越过 target owner"):
            registry.remove("config-target", owner="manual_crud")
        assert registry.has_target("config-target")
    finally:
        state.close()


def test_gateway_config_promotion_rejects_registry_revision_race(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.ensure_active_config_snapshot(
            config_domain="gateway",
            payload={"config_version": 1, "workspaces": []},
            source_baseline={},
            source_generation=0,
            layer_revisions={},
            layer_digests={},
            effective_digest="active-digest",
            schema_version=2,
        )
        pending = state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate-registry-race",
            idempotency_key="reload:registry-race",
            payload={"config_version": 1, "workspaces": []},
            source_baseline={},
            candidate_digest="candidate-digest",
            effective_digest="candidate-digest",
            target_generation="gateway-runtime",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=1,
        )
        claim = state.begin_config_apply(
            config_domain="gateway",
            candidate_id=pending.candidate_id,
            attempt_id="attempt-registry-race",
            apply_id="apply-registry-race",
            owner="gateway-config-service",
            base_active_revision=1,
            target_generation="gateway-runtime",
            pending_revision=pending.pending_revision,
            source_baseline={},
            active_baseline={},
            registry_revision=0,
        )

        state.replace_workspace_registry(
            {
                "schema_version": 10,
                "active_workspace_id": "manual-1",
                "targets": [
                    {
                        "workspace_id": "manual-1",
                        "owner": "manual",
                        "target_namespace": "gateway-manual",
                    }
                ],
                "remote_gateway_connections": [],
            },
            expected_revision=0,
            owner="manual_crud",
        )

        with pytest.raises(ConfigConflictError, match="registry revision CAS"):
            state.promote_active_config_snapshot(
                config_domain="gateway",
                candidate_id=pending.candidate_id,
                payload={"config_version": 1, "workspaces": []},
                source_baseline={},
                source_generation=0,
                layer_revisions={},
                layer_digests={},
                effective_digest="candidate-digest",
                schema_version=2,
                expected_active_revision=1,
                expected_pending_revision=pending.pending_revision,
                expected_fencing_token=claim.fencing_token,
                expected_registry_revision=0,
            )
        assert state.get_active_config_snapshot("gateway").effective_digest == (
            "active-digest"
        )
    finally:
        state.close()


def test_gateway_runtime_handoff_requires_healthy_reserved_target(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.record_gateway_runtime_generation(
            generation_id="gateway-old",
            process_id=100,
            loaded_source="active",
            candidate_id=None,
            active_revision=1,
            pending_revision=None,
            candidate_digest=None,
            effective_digest="active-digest",
            secret_binding_digest=None,
            fencing_token=None,
            listener_state="serving",
            state="active",
        )
        # fencing token 正确，但目标 generation 不是 healthy/reserved：
        # 必须命中专门的 healthy/reserved 预检错误，而不是落到最终 CAS 失败。
        for listener_state, target_state in (
            ("reserved", "starting"),
            ("serving", "active"),
            ("closed", "failed"),
        ):
            generation_id = f"gateway-{target_state}"
            state.record_gateway_runtime_generation(
                generation_id=generation_id,
                process_id=101,
                loaded_source="pending",
                candidate_id=None,
                active_revision=1,
                pending_revision=None,
                candidate_digest=None,
                effective_digest="pending-digest",
                secret_binding_digest=None,
                fencing_token="fence-ok",
                listener_state=listener_state,
                state=target_state,
            )
            with pytest.raises(ConfigConflictError, match="healthy/reserved"):
                state.handoff_gateway_runtime_generation(
                    generation_id=generation_id,
                    expected_old_generation="gateway-old",
                    fencing_token="fence-ok",
                )
    finally:
        state.close()


def test_gateway_runtime_generation_handoff_and_rollback_are_fenced(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        old = state.record_gateway_runtime_generation(
            generation_id="gateway-old",
            process_id=100,
            loaded_source="active",
            candidate_id=None,
            active_revision=1,
            pending_revision=None,
            candidate_digest=None,
            effective_digest="active-digest",
            secret_binding_digest="secret-digest",
            fencing_token=None,
            listener_state="serving",
            state="active",
        )
        new = state.record_gateway_runtime_generation(
            generation_id="gateway-new",
            process_id=101,
            loaded_source="pending",
            candidate_id="candidate-new",
            active_revision=1,
            pending_revision=2,
            candidate_digest="candidate-digest",
            effective_digest="candidate-digest",
            secret_binding_digest="secret-digest-2",
            fencing_token="fence-new",
            listener_state="reserved",
            state="healthy",
            health_proof={"protocol_version": FEDERATION_PROTOCOL_VERSION},
        )

        handed_off = state.handoff_gateway_runtime_generation(
            generation_id=new.generation_id,
            expected_old_generation=old.generation_id,
            fencing_token="fence-new",
        )
        assert handed_off.state == "active"
        assert handed_off.listener_state == "serving"
        draining = state.get_gateway_runtime_generation(
            generation_id=old.generation_id
        )
        assert draining is not None
        assert draining.state == "active"
        assert draining.listener_state == "draining"
        assert state.active_gateway_runtime_generation().generation_id == new.generation_id

        with pytest.raises(ConfigConflictError, match="fencing"):
            state.handoff_gateway_runtime_generation(
                generation_id=new.generation_id,
                expected_old_generation=old.generation_id,
                fencing_token="stale-fence",
            )

        failed, restored = state.rollback_gateway_runtime_handoff(
            generation_id=new.generation_id,
            old_generation_id=old.generation_id,
            fencing_token="fence-new",
        )
        assert failed.state == "failed"
        assert failed.listener_state == "closed"
        assert restored.state == "active"
        assert restored.listener_state == "serving"
        closed = state.close_gateway_runtime_generation(
            generation_id=old.generation_id,
            expected_states=("active",),
        )
        assert closed.state == "closed"
        assert closed.listener_state == "closed"
    finally:
        state.close()
