"""配置热重载设计验收矩阵与行为测试的可执行映射。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class AcceptanceCase:
    """一个设计验收场景及其行为测试入口。"""

    key: str
    test_file: str
    test_names: tuple[str, ...]


ACCEPTANCE_MATRIX = (
    AcceptanceCase(
        "file-api-conflict",
        "tests/unit/services/infrastructure/test_config_service.py",
        (
            "test_update_public_config_uses_runtime_overrides",
            "test_public_runtime_override_rejects_stale_layer_and_active_cas",
            "test_public_runtime_override_replay_is_idempotent",
            "test_source_change_during_apply_fails_final_cas_without_active_promotion",
            "test_file_change_after_parse_is_rejected_before_source_layer_commit",
        ),
    ),
    AcceptanceCase(
        "session-scope",
        "tests/unit/services/infrastructure/test_config_service.py",
        ("test_workspace_session_defaults_are_persisted_without_changing_static_config",),
    ),
    AcceptanceCase(
        "config-event-contract",
        "tests/contracts/api/test_config_reload_contract.py",
        (
            "test_workspace_and_gateway_event_schemas_expose_activation_scope",
            "test_pending_event_contract_never_reports_applied_paths",
        ),
    ),
    AcceptanceCase(
        "concurrent-toctou",
        "tests/unit/services/infrastructure/test_config_service.py",
        ("test_file_change_after_parse_is_rejected_before_source_layer_commit",),
    ),
    AcceptanceCase(
        "duplicate-watcher",
        "tests/unit/services/infrastructure/test_workspace_state_store.py",
        ("test_workspace_config_events_are_durable_replayable_and_idempotent",),
    ),
    AcceptanceCase(
        "mixed-modification-pending",
        "tests/unit/services/infrastructure/test_config_service.py",
        ("test_restart_required_reload_persists_pending_candidate_without_changing_active",),
    ),
    AcceptanceCase(
        "restart-failure-recovery",
        "tests/unit/gateway/test_gateway_config.py",
        (
            "test_gateway_config_reload_keeps_old_active_for_runtime_pending",
            "test_gateway_recovery_pending_requires_matching_proof_before_resolve",
        ),
    ),
    AcceptanceCase(
        "registry-reconcile",
        "tests/unit/gateway/test_registry.py",
        (
            "test_remote_projection_config_batch_is_atomic_and_respects_route_lease",
            "test_remote_projection_batch_handoff_closes_old_runtime_only_on_promotion",
        ),
    ),
    AcceptanceCase(
        "remote-delegation",
        "tests/unit/gateway/test_federation.py",
        (
            "test_configured_remote_reconcile_keeps_old_projection_when_peer_is_offline",
            "test_remote_projection_rejects_stale_cursor_and_accepts_full_snapshot_jump",
        ),
    ),
    AcceptanceCase(
        "diagnostics-read-only",
        "tests/unit/services/infrastructure/test_config_service.py",
        ("test_source_diagnostics_is_read_only_before_config_initialization",),
    ),
    AcceptanceCase(
        "pending-real-load",
        "tests/unit/services/infrastructure/test_config_service.py",
        (
            "test_workspace_pending_candidate_loader_requires_exact_startup_contract",
            "test_workspace_pending_candidate_loader_rejects_wrong_generation_or_token",
        ),
    ),
    AcceptanceCase(
        "workspace-user-fanout",
        "tests/unit/services/infrastructure/test_workspace_source_owner.py",
        (
            "test_config_service_materializes_shared_user_source_generation",
            "test_shared_source_owner_prepares_stopped_workspace_and_preserves_failed_result",
        ),
    ),
    AcceptanceCase(
        "gateway-connection-identity",
        "tests/unit/gateway/test_gateway_config.py",
        (
            "test_gateway_connection_id_migration_is_comment_preserving_and_idempotent",
            "test_gateway_connection_id_migration_recovers_after_file_write_crash",
        ),
    ),
    AcceptanceCase(
        "gateway-pending-restart",
        "tests/integration/gateway/test_gateway_workspace_routing.py",
        (
            "test_gateway_pending_restart_is_loaded_by_new_gateway_process",
            "test_gateway_pending_startup_failure_keeps_active_snapshot_recoverable",
        ),
    ),
    AcceptanceCase(
        "gateway-pending-identity-expiry",
        "tests/integration/gateway/test_gateway_workspace_routing.py",
        (
            "test_gateway_expired_pending_requires_explicit_retry_before_startup",
            "test_gateway_stale_pending_startup_cannot_mutate_restart_state",
        ),
    ),
    AcceptanceCase(
        "source-change-during-apply",
        "tests/unit/services/infrastructure/test_config_service.py",
        (
            "test_source_change_during_apply_without_side_effect_fails_before_promotion",
            "test_source_change_during_apply_fails_final_cas_without_active_promotion",
        ),
    ),
    AcceptanceCase(
        "jsonc-deletion-rename",
        "tests/unit/services/infrastructure/test_config_service.py",
        (
            "test_sqlite_migrated_source_deletion_writes_absent_tombstone",
            "test_jsonc_rename_to_temp_creates_tombstone_and_restore_reimports_source",
        ),
    ),
    AcceptanceCase(
        "active-snapshot-recovery",
        "tests/unit/services/infrastructure/test_config_service.py",
        ("test_config_active_snapshot_is_persisted_redacted_and_restored",),
    ),
    AcceptanceCase(
        "secret-compatibility-rotation",
        "tests/unit/services/infrastructure/test_workspace_state_store.py",
        (
            "test_legacy_workspace_secret_migration_blocks_literal_and_normalizes_env",
            "test_secret_binding_resolver_reports_failure_and_detects_rotation",
        ),
    ),
    AcceptanceCase(
        "source-a-b-a",
        "tests/unit/services/infrastructure/test_workspace_source_owner.py",
        (
            "test_shared_source_owner_distinguishes_a_b_a_and_deduplicates_adjacent_observation",
            "test_shared_source_owner_prepares_stopped_workspace_and_preserves_failed_result",
            "test_config_service_catches_up_stopped_workspace_from_source_high_water",
        ),
    ),
    AcceptanceCase(
        "registry-manual-concurrency",
        "tests/unit/gateway/test_gateway_state.py",
        (
            "test_gateway_registry_manual_mutation_preserves_config_targets",
            "test_gateway_config_promotion_rejects_registry_revision_race",
            "test_gateway_registry_apply_journal_recovery_is_explicit",
        ),
    ),
)


def _test_function_names(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name.startswith("test_")
    )


@pytest.mark.parametrize(
    "acceptance_case",
    ACCEPTANCE_MATRIX,
    ids=lambda acceptance_case: acceptance_case.key,
)
def test_acceptance_matrix_references_existing_behavior_tests(
    acceptance_case: AcceptanceCase,
) -> None:
    """矩阵中的每个场景必须绑定到仓库内可收集的行为测试。"""

    test_path = Path.cwd() / acceptance_case.test_file
    assert test_path.is_file(), f"验收场景缺少测试文件: {test_path}"
    available_names = _test_function_names(test_path)
    missing_names = set(acceptance_case.test_names) - available_names
    assert not missing_names, (
        f"验收场景 {acceptance_case.key} 引用了不存在的行为测试: "
        f"{sorted(missing_names)}"
    )
