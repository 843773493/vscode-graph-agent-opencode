# R21 实施记录

## 历史记录：catalog resolver 读取已发布 child thread（非本次 integration 迁移）

## 变更

- 在 `SessionControlStore` 增加 `get_published_child_thread_locator`，把
  `thread_catalog` 的 child 可见性与同一发布事务冻结的
  `thread_creation_records.final_relative_locator` 做一致性校验。
- `SessionCatalogPathResolver.resolve_thread_node` 的非 main 分支改为读取
  owner Session 的 `session-control.sqlite`，按冻结 locator 定位物理目录。
  解析过程不再扫描 `threads/`、按日期桶猜测路径或吸收未登记目录。
- 增加已发布 child、缺少 control 数据、未登记目录和已发布目录缺失等单测。

## 验证

- `uv run pytest tests/unit/core/test_session_catalog_resolver.py tests/unit/core/test_session_control_store.py -q --tb=short`
  - 144 passed
- `uv run pytest tests/unit/core/ -q --tb=short`
  - 678 passed
- `uv run ruff check app/core/session_control_store.py app/core/session_catalog_resolver.py tests/unit/core/test_session_catalog_resolver.py`
  - All checks passed
- `uv run python -m compileall -q app/core tests/unit/core/test_session_catalog_resolver.py`
  - exit 0

## 边界

R21 只打通 child thread 的权威定位读取，不接入 delegate 调用方、Job
execution、真实 ContextStore/rollout 文件或 Web 面板；这些仍属于后续 8.5/8.3
切片。旧 `SessionPathResolver` 未修改。


## R21 integration catalog 测试迁移（2026-09-18 新指令）

此前本文记录的是 child thread locator 生产变更，以下单独记录本次测试面任务，不覆盖历史记录。

### 已确认范围与选型

- 以最新 AGENTS.md 与实施委派为准，旧任务书 S1/S2 的别名映射或绕过校验登记方案作废。
- 删除未被任何测试调用的 `tests/harness/python/canonical_ids.py`；不创建映射 helper，不修改 factory，也不改变 catalog/legacy 分支。
- 直接使用现有 canonical 常量，或 `app.core.identifier.create_prefixed_id("ses")` 入口，迁移调用方身份及关联断言。
- `app/**`、OpenSpec 勾选和既有 dirty diff 不属于本轮改动。
- 已留存迁移前 app diff 与 checkpoint 文件原有 diff，最终会比较本轮增量。

### 试点：checkpoint message groups

- 仅将 `group_case.session_id` 从旧非法值替换为仓库现有 canonical 常量 `ses_e6d2707870e54cab8c135193c0802532`。
- 原有 `ensure_request_items` / model-call tool identity 改动完整保留；metadata 中仅用于测试透传的 `parent-1` 不属于真实会话定位路径，不改语义。
- `uv run pytest tests/integration/backend/sessions/test_checkpoint_message_groups.py -q --tb=short`：18 passed（10.31s）。
- `uv run ruff check tests/integration/backend/sessions/test_checkpoint_message_groups.py`：All checks passed。
- 产物：`artifacts/r21-checkpoint-pilot.log`、`artifacts/r21-checkpoint-ruff.log`。

### 基线

- 默认 catalog 完整基线：215 failed / 52 passed / 892 errors（628.21s）。
- 基线日志已从错误的 `rounds/artifacts/` 移到 `out/tests/temp/context-lifecycle-team/artifacts/r21-integration-catalog-before.log`。
- 215F 逐项归因见下节；双模式 delta 归因与残差分类见文末「最终双模式验证」。

### 215F 逐项归因（完整 before.log 的失败顺序与错误一一对应）

`--tb=line` 的 FAILURES 段恰含 215 条 `E`，summary 恰含 215 个 FAILED nodeid；下表按相同 pytest 报告顺序配对，无按 ERROR 家族推断。

A=固定非法会话 ID；C=随机前缀非法 ID；B/workspace_id=未走 factory 的 workspace identity 不一致；既有债务=当前 API/路径断言失败。

| # | nodeid | 原因 | 实际错误 |
| --- | --- | --- | --- |
| 1 | rollout_context/test_itemized_migration_legacy.py::test_public_v1_report_is_sanitized_and_v2_runtime_is_strict | C | ValueError: session_id 形态非法: 'legacy-source-11f0e76226a04b48a41d3b76b3324354' |
| 2 | rollout_context/test_itemized_migration_legacy.py::test_success_installs_only_v2_and_preserves_final_lineage | C | ValueError: session_id 形态非法: 'legacy-source-587b7dc3b4a549ff8625f022d3df6f84' |
| 3 | rollout_context/test_itemized_migration_legacy.py::test_missing_final_marker_is_unknown_and_has_no_final_item | C | ValueError: session_id 形态非法: 'legacy-source-0991723dade6485798c2a7f52250a4a7' |
| 4 | rollout_context/test_itemized_migration_legacy.py::test_rejected_candidate_is_quarantined_without_lossless_claim | C | ValueError: session_id 形态非法: 'legacy-source-79d3c9743b28462d8a541bf243bb5a91' |
| 5 | rollout_context/test_itemized_migration_legacy.py::test_require_lossless_failure_keeps_source_snapshot_and_staging_audit | C | ValueError: session_id 形态非法: 'legacy-source-2dd4cb36b9c044a39d9dc95bbf8ce9e4' |
| 6 | rollout_context/test_itemized_migration_legacy.py::test_precommit_validation_failure_never_installs_target | C | ValueError: session_id 形态非法: 'legacy-source-f20b21f609eb47fb98109b1137fe3d41' |
| 7 | rollout_context/test_itemized_migration_legacy.py::test_restart_recovers_only_audit_and_then_installs_v2[during_build] | C | ValueError: session_id 形态非法: 'legacy-source-823c155681d246948814ca732f132efd' |
| 8 | rollout_context/test_itemized_migration_legacy.py::test_restart_recovers_only_audit_and_then_installs_v2[after_install] | C | ValueError: session_id 形态非法: 'legacy-source-c873047fff0b4fe3a4562bd728d2da02' |
| 9 | rollout_context/test_itemized_migration_legacy.py::test_symlink_boundaries_are_rejected_before_external_mutation[source_jsonl] | C | ValueError: session_id 形态非法: 'legacy-source-fe6aa1dfec2742258d455b241ddc53bd' |
| 10 | rollout_context/test_itemized_migration_legacy.py::test_symlink_boundaries_are_rejected_before_external_mutation[source_root] | C | ValueError: session_id 形态非法: 'legacy-source-1f5c2f5986904dadabe8409168d92691' |
| 11 | rollout_context/test_itemized_migration_legacy.py::test_symlink_boundaries_are_rejected_before_external_mutation[target_root] | C | ValueError: session_id 形态非法: 'legacy-source-3ee63294f943486380aaf281052db7c3' |
| 12 | rollout_context/test_itemized_migration_legacy.py::test_symlink_boundaries_are_rejected_before_external_mutation[audit_root] | C | ValueError: session_id 形态非法: 'legacy-source-3b2a4fe272944e63b7e547d708952e7c' |
| 13 | rollout_context/test_itemized_migration_legacy.py::test_hardlink_source_is_rejected_without_creating_target | C | ValueError: session_id 形态非法: 'legacy-source-2c45ff6f6a3d4fd3a7bb3d1cd83eaf00' |
| 14 | rollout_context/test_itemized_migration_legacy.py::test_uncommitted_tail_and_overlay_rows_are_raw_loss_not_v2_history | C | ValueError: session_id 形态非法: 'legacy-source-f41944c0f91f448db8409e28d487ac94' |
| 15 | rollout_context/test_itemized_migration_legacy.py::test_manifest_envelope_mismatch_fails_with_original_and_audit_preserved | C | ValueError: session_id 形态非法: 'legacy-source-460b84edf02d4b409753e6c9fce3140a' |
| 16 | rollout_context/test_rollout_checkpoint_saver.py::test_rollout_preserves_langchain_invalid_tool_calls_field | A | ValueError: session_id 形态非法: 'session_1' |
| 17 | rollout_context/test_rollout_checkpoint_saver.py::test_put_treats_unpersisted_initial_parent_as_root | A | ValueError: session_id 形态非法: 'session_1' |
| 18 | rollout_context/test_rollout_checkpoint_saver.py::test_put_repairs_empty_message_channel_version | A | ValueError: session_id 形态非法: 'session_1' |
| 19 | rollout_context/test_rollout_checkpoint_saver.py::test_checkpoint_namespace_queries_do_not_cross_match | A | ValueError: session_id 形态非法: 'session_1' |
| 20 | rollout_context/test_rollout_checkpoint_saver.py::test_read_snapshot_keeps_sqlite_and_jsonl_watermark_consistent_across_processes | A | ValueError: session_id 形态非法: 'session_1' |
| 21 | rollout_context/test_rollout_checkpoint_saver.py::test_read_snapshot_does_not_touch_existing_rollout_files | A | ValueError: session_id 形态非法: 'session_1' |
| 22 | rollout_context/test_rollout_checkpoint_saver.py::test_checkpoint_read_does_not_reinitialize_existing_rollout | A | ValueError: session_id 形态非法: 'session_1' |
| 23 | rollout_context/test_rollout_checkpoint_saver.py::test_delete_legacy_rollout_does_not_initialize_removed_layout | A | ValueError: session_id 形态非法: 'session_legacy' |
| 24 | rollout_context/test_rollout_checkpoint_saver.py::test_validate_index_uses_read_only_snapshot_for_maintenance_check | A | ValueError: session_id 形态非法: 'session_1' |
| 25 | rollout_context/test_rollout_checkpoint_saver.py::test_history_page_uses_one_snapshot_and_sqlite_keyset_window | A | ValueError: session_id 形态非法: 'session_1' |
| 26 | rollout_context/test_rollout_checkpoint_saver.py::test_history_index_failure_closes_read_snapshot | A | ValueError: session_id 形态非法: 'session_1' |
| 27 | rollout_context/test_rollout_checkpoint_saver.py::test_checkpoint_envelope_and_channels_are_authoritative_sqlite | A | ValueError: session_id 形态非法: 'session_1' |
| 28 | rollout_context/test_rollout_checkpoint_saver.py::test_failed_turn_status_survives_history_reload | A | ValueError: session_id 形态非法: 'session_1' |
| 29 | rollout_context/test_rollout_checkpoint_saver.py::test_terminal_turn_status_convergence_is_idempotent_after_termination | A | ValueError: session_id 形态非法: 'session_1' |
| 30 | rollout_context/test_rollout_checkpoint_saver.py::test_hidden_system_reminder_does_not_create_empty_chat_turn | A | ValueError: session_id 形态非法: 'session_1' |
| 31 | rollout_context/test_rollout_checkpoint_saver.py::test_legacy_hidden_reminder_turn_is_excluded_from_history | A | ValueError: session_id 形态非法: 'session_1' |
| 32 | rollout_context/test_rollout_checkpoint_saver.py::test_rewind_replay_uses_new_canonical_suffix_without_replacement_event | A | ValueError: session_id 形态非法: 'session_1' |
| 33 | rollout_context/test_rollout_checkpoint_saver.py::test_pending_writes_and_all_checkpoint_channels_round_trip | A | ValueError: session_id 形态非法: 'session_1' |
| 34 | rollout_context/test_rollout_checkpoint_saver.py::test_pending_write_corruption_is_rejected_instead_of_decoded | A | ValueError: session_id 形态非法: 'session_1' |
| 35 | rollout_context/test_rollout_checkpoint_saver.py::test_uncommitted_jsonl_tail_is_truncated_but_sqlite_loss_is_explicit_failure | A | ValueError: session_id 形态非法: 'session_1' |
| 36 | rollout_context/test_rollout_checkpoint_saver.py::test_half_line_and_fsync_failure_never_become_committed_messages | A | ValueError: session_id 形态非法: 'session_1' |
| 37 | rollout_context/test_rollout_checkpoint_saver.py::test_sqlite_commit_window_is_retryable_without_duplicate_jsonl | A | ValueError: session_id 形态非法: 'session_1' |
| 38 | rollout_context/test_rollout_checkpoint_saver.py::test_concurrent_checkpoint_appends_do_not_interleave_jsonl | A | ValueError: session_id 形态非法: 'session_1' |
| 39 | rollout_context/test_rollout_checkpoint_saver.py::test_repeating_same_checkpoint_is_idempotent_but_conflicting_payload_fails | A | ValueError: session_id 形态非法: 'session_1' |
| 40 | rollout_context/test_rollout_checkpoint_saver.py::test_checkpoint_retry_rejects_corrupted_commit_pointer | A | ValueError: session_id 形态非法: 'session_1' |
| 41 | rollout_context/test_rollout_checkpoint_saver.py::test_checkpoint_view_without_new_items_reuses_previous_commit | A | ValueError: session_id 形态非法: 'session_1' |
| 42 | rollout_context/test_rollout_checkpoint_saver.py::test_read_rejects_broken_storage_commit_offset_chain | A | ValueError: session_id 形态非法: 'session_1' |
| 43 | rollout_context/test_rollout_checkpoint_saver.py::test_read_rejects_storage_commit_with_incomplete_item_catalog | A | ValueError: session_id 形态非法: 'session_1' |
| 44 | rollout_context/test_rollout_checkpoint_saver.py::test_read_rejects_catalog_locator_that_does_not_match_canonical_jsonl | A | ValueError: session_id 形态非法: 'session_1' |
| 45 | rollout_context/test_rollout_checkpoint_saver.py::test_rollout_schema_exposes_all_authoritative_tables_and_core_constraints | A | ValueError: session_id 形态非法: 'session_1' |
| 46 | rollout_context/test_rollout_checkpoint_saver.py::test_sqlite_backup_restores_authoritative_checkpoint_state | A | ValueError: session_id 形态非法: 'session_1' |
| 47 | rollout_context/test_rollout_checkpoint_saver.py::test_sqlite_backup_failure_does_not_leave_partial_temporary_index | A | ValueError: session_id 形态非法: 'session_1' |
| 48 | rollout_context/test_rollout_checkpoint_saver.py::test_sqlite_restore_failure_does_not_leave_partial_temporary_index | A | ValueError: session_id 形态非法: 'session_1' |
| 49 | rollout_context/test_rollout_checkpoint_saver.py::test_schema_migration_checksum_and_completion_are_authoritative | A | ValueError: session_id 形态非法: 'session_1' |
| 50 | rollout_context/test_rollout_checkpoint_saver.py::test_schema_migration_runs_transactionally_and_keeps_backup | A | ValueError: session_id 形态非法: 'session_1' |
| 51 | rollout_context/test_rollout_checkpoint_saver.py::test_failed_schema_migration_restores_backup_and_requires_recovery | A | ValueError: session_id 形态非法: 'session_1' |
| 52 | rollout_context/test_turn_execution_recovery.py::test_acceptance_retry_rejects_corrupted_idempotency_ledger[status-<lambda>-\u5c1a\u672a committed\|\u672a\u6536\u655b] | A | ValueError: session_id 形态非法: 'session_1' |
| 53 | rollout_context/test_turn_execution_recovery.py::test_acceptance_retry_rejects_corrupted_idempotency_ledger[metadata_json-<lambda>-metadata_json] | A | ValueError: session_id 形态非法: 'session_1' |
| 54 | rollout_context/test_turn_execution_recovery.py::test_acceptance_retry_rejects_corrupted_idempotency_ledger[jsonl_offset_after-<lambda>-offset \u5b57\u6bb5\u4e0d\u4e00\u81f4\|\u4e0e end \u51b2\u7a81] | A | ValueError: session_id 形态非法: 'session_1' |
| 55 | rollout_context/test_turn_execution_recovery.py::test_item_commit_rejects_non_canonical_locator_metadata[metadata0] | A | ValueError: session_id 形态非法: 'session_1' |
| 56 | rollout_context/test_turn_execution_recovery.py::test_item_commit_rejects_non_canonical_locator_metadata[metadata1] | A | ValueError: session_id 形态非法: 'session_1' |
| 57 | rollout_context/test_turn_execution_recovery.py::test_item_commit_rejects_non_canonical_locator_metadata[metadata2] | A | ValueError: session_id 形态非法: 'session_1' |
| 58 | rollout_context/test_turn_execution_recovery.py::test_item_commit_rejects_non_canonical_locator_metadata[metadata3] | A | ValueError: session_id 形态非法: 'session_1' |
| 59 | rollout_context/test_turn_execution_recovery.py::test_item_commit_uses_canonical_writer_for_block_part_projection | A | ValueError: session_id 形态非法: 'session_1' |
| 60 | rollout_context/test_turn_execution_recovery.py::test_acceptance_and_provider_retry_keep_one_real_user_root | A | ValueError: session_id 形态非法: 'session_1' |
| 61 | rollout_context/test_turn_execution_recovery.py::test_turn_projection_uses_canonical_tool_identity_before_message_projection | A | ValueError: session_id 形态非法: 'session_1' |
| 62 | rollout_context/test_turn_execution_recovery.py::test_canonical_tool_call_recovers_coordinates_from_scoped_block_id | A | ValueError: session_id 形态非法: 'session_1' |
| 63 | rollout_context/test_turn_execution_recovery.py::test_item_catalog_reads_fail_closed_on_missing_item_and_view_reference | A | ValueError: session_id 形态非法: 'session_1' |
| 64 | rollout_context/test_turn_execution_recovery.py::test_index_validation_rejects_catalog_sequence_gaps_even_when_jsonl_matches | A | ValueError: session_id 形态非法: 'session_1' |
| 65 | rollout_context/test_turn_execution_recovery.py::test_item_projection_read_rejects_catalog_hash_drift | A | ValueError: session_id 形态非法: 'session_1' |
| 66 | rollout_context/test_turn_execution_recovery.py::test_item_projection_read_rejects_typed_or_identity_drift[status-failed] | A | ValueError: session_id 形态非法: 'session_1' |
| 67 | rollout_context/test_turn_execution_recovery.py::test_item_projection_read_rejects_typed_or_identity_drift[content_length-999] | A | ValueError: session_id 形态非法: 'session_1' |
| 68 | rollout_context/test_turn_execution_recovery.py::test_item_projection_read_rejects_typed_or_identity_drift[content_truncated-2] | A | ValueError: session_id 形态非法: 'session_1' |
| 69 | rollout_context/test_turn_execution_recovery.py::test_item_projection_read_rejects_typed_or_identity_drift[projection_version-broken] | A | ValueError: session_id 形态非法: 'session_1' |
| 70 | rollout_context/test_turn_execution_recovery.py::test_item_projection_read_rejects_typed_or_identity_drift[id-projection-alias] | A | ValueError: session_id 形态非法: 'session_1' |
| 71 | rollout_context/test_turn_execution_recovery.py::test_execution_lost_resume_creates_execution_without_a_new_user_root | A | ValueError: session_id 形态非法: 'session_1' |
| 72 | rollout_context/test_turn_execution_recovery.py::test_interrupted_turn_resumes_after_restart_without_new_user_root | A | ValueError: session_id 形态非法: 'session_1' |
| 73 | rollout_context/test_turn_execution_recovery.py::test_partial_stream_item_and_anchor_survive_runtime_restart | A | ValueError: session_id 形态非法: 'session_1' |
| 74 | rollout_context/test_turn_execution_recovery.py::test_checkpoint_first_root_creates_one_based_turn_ordinal | A | ValueError: session_id 形态非法: 'session_1' |
| 75 | rollout_context/test_turn_execution_recovery.py::test_committed_overlay_selection_is_epoch_stable_after_restart | A | ValueError: session_id 形态非法: 'session_1' |
| 76 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[source_overlay_epoch-True] | A | ValueError: session_id 形态非法: 'session_1' |
| 77 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[source_overlay_epoch-0] | A | ValueError: session_id 形态非法: 'session_1' |
| 78 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[checkpoint_ns-1] | A | ValueError: session_id 形态非法: 'session_1' |
| 79 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[base_content_length-not-a-length] | A | ValueError: session_id 形态非法: 'session_1' |
| 80 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[base_content_hash-123] | A | ValueError: session_id 形态非法: 'session_1' |
| 81 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[base_source_revision-123] | A | ValueError: session_id 形态非法: 'session_1' |
| 82 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[idempotency_key-123] | A | ValueError: session_id 形态非法: 'session_1' |
| 83 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_coerced_manifest_values[status-corrupted] | A | ValueError: session_id 形态非法: 'session_1' |
| 84 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_registration_rejects_ref_identity_collision | A | ValueError: session_id 形态非法: 'session_1' |
| 85 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_restore_rejects_corrupted_registry_rows[status-corrupted] | A | ValueError: session_id 形态非法: 'session_1' |
| 86 | rollout_context/test_turn_execution_recovery.py::test_source_overlay_restore_rejects_corrupted_registry_rows[source_overlay_epoch-not-an-epoch] | A | ValueError: session_id 形态非法: 'session_1' |
| 87 | rollout_context/test_turn_execution_recovery.py::test_item_bearing_terminal_convergence_materializes_output_and_restart | A | ValueError: session_id 形态非法: 'session_1' |
| 88 | rollout_context/test_turn_execution_recovery.py::test_item_bearing_terminal_projection_failure_rolls_back_jsonl_and_sqlite | A | ValueError: session_id 形态非法: 'session_1' |
| 89 | rollout_context/test_turn_execution_recovery.py::test_cancelled_turn_rejects_original_dispatch_and_replays_as_new_turn | A | ValueError: session_id 形态非法: 'session_1' |
| 90 | rollout_context/test_turn_execution_recovery.py::test_terminal_turn_rejects_resume_and_original_dispatch_without_mutation[completed_empty-completed_empty] | A | ValueError: session_id 形态非法: 'session_1' |
| 91 | rollout_context/test_turn_execution_recovery.py::test_terminal_turn_rejects_resume_and_original_dispatch_without_mutation[failed-failed] | A | ValueError: session_id 形态非法: 'session_1' |
| 92 | rollout_context/test_turn_execution_recovery.py::test_replay_as_new_turn_is_idempotent_after_saver_restart | A | ValueError: session_id 形态非法: 'session_1' |
| 93 | rollout_context/test_turn_execution_recovery.py::test_partial_content_part_anchor_survives_restart_and_rejects_unreachable_view | A | ValueError: session_id 形态非法: 'session_1' |
| 94 | rollout_context/test_turn_execution_recovery.py::test_sealed_selection_drives_langchain_and_provider_projection_after_restart | A | ValueError: session_id 形态非法: 'session_1' |
| 95 | rollout_context/test_turn_execution_recovery.py::test_optional_omitted_selection_skips_detail_body_and_provider_tools | A | ValueError: session_id 形态非法: 'session_1' |
| 96 | rollout_context/test_turn_execution_recovery.py::test_request_only_detail_and_selection_restore_without_memory_body | A | ValueError: session_id 形态非法: 'session_1' |
| 97 | rollout_context/test_turn_execution_recovery.py::test_protected_request_only_detail_projects_after_restart_with_injected_key | A | ValueError: session_id 形态非法: 'session_1' |
| 98 | rollout_context/test_turn_execution_recovery.py::test_sealed_assembly_restore_rejects_coerced_sqlite_manifest_values[context_assemblies-status-1] | A | ValueError: session_id 形态非法: 'session_1' |
| 99 | rollout_context/test_turn_execution_recovery.py::test_sealed_assembly_restore_rejects_coerced_sqlite_manifest_values[context_assemblies-history_view_revision--1] | A | ValueError: session_id 形态非法: 'session_1' |
| 100 | rollout_context/test_turn_execution_recovery.py::test_sealed_assembly_restore_rejects_coerced_sqlite_manifest_values[assembly_item_refs-content_length--1] | A | ValueError: session_id 形态非法: 'session_1' |
| 101 | rollout_context/test_turn_execution_recovery.py::test_sealed_assembly_restore_rejects_missing_detail_manifest | A | ValueError: session_id 形态非法: 'session_1' |
| 102 | test_config_migration_session_retry.py::test_persisted_failed_session_retries_with_migrated_current_tools | 既有债务 | Failed: 等待旧配置热迁移超时 |
| 103 | test_itemized_migration.py::test_v1_report_is_read_only_and_migration_installs_target_v2 | A | ValueError: session_id 形态非法: 'source' |
| 104 | test_itemized_migration.py::test_legacy_migration_rejects_same_source_and_target_session | A | ValueError: session_id 形态非法: 'source' |
| 105 | test_itemized_migration.py::test_full_copy_v1_source_requires_explicit_import_without_side_effects | A | ValueError: session_id 形态非法: 'source' |
| 106 | test_itemized_migration.py::test_explicit_v1_import_then_full_copy_records_v2_source_lineage | A | ValueError: session_id 形态非法: 'source' |
| 107 | test_itemized_migration.py::test_migration_failure_quarantines_staging_without_creating_target[projection] | A | ValueError: session_id 形态非法: 'source' |
| 108 | test_itemized_migration.py::test_migration_failure_quarantines_staging_without_creating_target[validation] | A | ValueError: session_id 形态非法: 'source' |
| 109 | test_itemized_migration.py::test_migration_failure_quarantines_staging_without_creating_target[installation] | A | ValueError: session_id 形态非法: 'source' |
| 110 | test_itemized_migration.py::test_migration_target_remains_absent_until_validated_directory_install | A | ValueError: session_id 形态非法: 'source' |
| 111 | test_itemized_migration.py::test_corrupt_source_is_rejected_and_only_failure_audit_is_written[UPDATE messages SET message_id='mismatch' WHERE message_sequence=1-manifest \u4e0e JSONL] | A | ValueError: session_id 形态非法: 'source' |
| 112 | test_itemized_migration.py::test_corrupt_source_is_rejected_and_only_failure_audit_is_written[UPDATE messages SET jsonl_length='not-an-integer' WHERE message_sequence=1-jsonl_length] | A | ValueError: session_id 形态非法: 'source' |
| 113 | test_itemized_migration.py::test_corrupt_source_is_rejected_and_only_failure_audit_is_written[UPDATE database_meta SET committed_jsonl_offset='not-an-integer'-committed_jsonl_offset] | A | ValueError: session_id 形态非法: 'source' |
| 114 | test_itemized_migration.py::test_corrupt_source_is_rejected_and_only_failure_audit_is_written[UPDATE database_meta SET rollout_format_version=9-rollout_format_version] | A | ValueError: session_id 形态非法: 'source' |
| 115 | test_itemized_migration.py::test_corrupt_source_is_rejected_and_only_failure_audit_is_written[ALTER TABLE database_meta DROP COLUMN rollout_format_version-\u7f3a\u5c11\u5b57\u6bb5] | A | ValueError: session_id 形态非法: 'source' |
| 116 | test_itemized_migration.py::test_unknown_or_mixed_envelope_format_is_not_coerced[9] | A | ValueError: session_id 形态非法: 'source' |
| 117 | test_itemized_migration.py::test_unknown_or_mixed_envelope_format_is_not_coerced[2] | A | ValueError: session_id 形态非法: 'source' |
| 118 | test_itemized_migration.py::test_unknown_or_mixed_envelope_format_is_not_coerced[True] | A | ValueError: session_id 形态非法: 'source' |
| 119 | test_itemized_migration.py::test_unknown_or_mixed_envelope_format_is_not_coerced[1] | A | ValueError: session_id 形态非法: 'source' |
| 120 | test_itemized_migration.py::test_unknown_or_mixed_envelope_format_is_not_coerced[1.0] | A | ValueError: session_id 形态非法: 'source' |
| 121 | test_itemized_migration.py::test_finalization_requires_unambiguous_legacy_evidence[metadata0-unknown] | A | ValueError: session_id 形态非法: 'source' |
| 122 | test_itemized_migration.py::test_finalization_requires_unambiguous_legacy_evidence[metadata1-completed] | A | ValueError: session_id 形态非法: 'source' |
| 123 | test_itemized_migration.py::test_finalization_requires_unambiguous_legacy_evidence[metadata2-failed] | A | ValueError: session_id 形态非法: 'source' |
| 124 | test_itemized_migration.py::test_finalization_requires_unambiguous_legacy_evidence[metadata3-interrupted] | A | ValueError: session_id 形态非法: 'source' |
| 125 | test_itemized_migration.py::test_finalization_requires_unambiguous_legacy_evidence[metadata4-cancelled] | A | ValueError: session_id 形态非法: 'source' |
| 126 | test_itemized_migration.py::test_finalization_requires_unambiguous_legacy_evidence[metadata5-unknown] | A | ValueError: session_id 形态非法: 'source' |
| 127 | test_itemized_migration_integrity.py::test_preexisting_target_is_preserved[empty_v2] | A | ValueError: session_id 形态非法: 'source' |
| 128 | test_itemized_migration_integrity.py::test_preexisting_target_is_preserved[partial] | A | ValueError: session_id 形态非法: 'source' |
| 129 | test_itemized_migration_integrity.py::test_preexisting_target_is_preserved[empty_directory] | A | ValueError: session_id 形态非法: 'source' |
| 130 | test_itemized_migration_integrity.py::test_symlink_path_is_rejected_without_touching_external_files[source_jsonl] | A | ValueError: session_id 形态非法: 'source' |
| 131 | test_itemized_migration_integrity.py::test_symlink_path_is_rejected_without_touching_external_files[source_index] | A | ValueError: session_id 形态非法: 'source' |
| 132 | test_itemized_migration_integrity.py::test_symlink_path_is_rejected_without_touching_external_files[source_root] | A | ValueError: session_id 形态非法: 'source' |
| 133 | test_itemized_migration_integrity.py::test_symlink_path_is_rejected_without_touching_external_files[target_root] | A | ValueError: session_id 形态非法: 'source' |
| 134 | test_itemized_migration_integrity.py::test_symlink_path_is_rejected_without_touching_external_files[audit_root] | A | ValueError: session_id 形态非法: 'source' |
| 135 | test_itemized_migration_integrity.py::test_symlink_path_is_rejected_without_touching_external_files[target_lock] | A | ValueError: session_id 形态非法: 'source' |
| 136 | test_itemized_migration_integrity.py::test_hardlink_source_is_rejected | A | ValueError: session_id 形态非法: 'source' |
| 137 | test_itemized_migration_integrity.py::test_process_exit_preserves_originals_and_recovers_only_audit[during_build] | A | ValueError: session_id 形态非法: 'source' |
| 138 | test_itemized_migration_integrity.py::test_process_exit_preserves_originals_and_recovers_only_audit[before_install] | A | ValueError: session_id 形态非法: 'source' |
| 139 | test_itemized_migration_integrity.py::test_process_exit_preserves_originals_and_recovers_only_audit[after_install] | A | ValueError: session_id 形态非法: 'source' |
| 140 | test_itemized_migration_integrity.py::test_post_install_error_cannot_erase_published_history | A | ValueError: session_id 形态非法: 'source' |
| 141 | test_itemized_migration_integrity.py::test_source_change_before_install_is_detected | A | ValueError: session_id 形态非法: 'source' |
| 142 | test_itemized_migration_integrity.py::test_recovery_rejects_replaced_artifact_symlink[index.sqlite] | A | ValueError: session_id 形态非法: 'source' |
| 143 | test_itemized_migration_integrity.py::test_recovery_rejects_replaced_artifact_symlink[rollout.jsonl] | A | ValueError: session_id 形态非法: 'source' |
| 144 | test_itemized_migration_integrity.py::test_final_marker_cannot_hide_failed_message_state[status] | A | ValueError: session_id 形态非法: 'source' |
| 145 | test_itemized_migration_integrity.py::test_final_marker_cannot_hide_failed_message_state[outcome] | A | ValueError: session_id 形态非法: 'source' |
| 146 | test_itemized_migration_integrity.py::test_unmapped_metadata_is_protected_and_explicitly_lossy | A | ValueError: session_id 形态非法: 'source' |
| 147 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[overlap] | A | ValueError: session_id 形态非法: 'source' |
| 148 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[gap] | A | ValueError: session_id 形态非法: 'source' |
| 149 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[trailing_committed] | A | ValueError: session_id 形态非法: 'source' |
| 150 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[bad_hash] | A | ValueError: session_id 形态非法: 'source' |
| 151 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[duplicate_key] | A | ValueError: session_id 形态非法: 'source' |
| 152 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[missing_turn] | A | ValueError: session_id 形态非法: 'source' |
| 153 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[bad_type] | A | ValueError: session_id 形态非法: 'source' |
| 154 | test_itemized_migration_integrity.py::test_strict_manifest_envelope_coordinates[unfinished_line] | A | ValueError: session_id 形态非法: 'source' |
| 155 | test_itemized_migration_integrity.py::test_multiple_final_markers_converge_unknown | A | ValueError: session_id 形态非法: 'source' |
| 156 | test_itemized_migration_integrity.py::test_legacy_hashes_and_source_coordinates_are_frozen | A | ValueError: session_id 形态非法: 'source' |
| 157 | test_itemized_migration_integrity.py::test_raw_quarantine_is_complete_private_and_reported[unknown_role] | A | ValueError: session_id 形态非法: 'source' |
| 158 | test_itemized_migration_integrity.py::test_raw_quarantine_is_complete_private_and_reported[wrong_carrier] | A | ValueError: session_id 形态非法: 'source' |
| 159 | test_itemized_migration_integrity.py::test_raw_quarantine_is_complete_private_and_reported[protected_reasoning] | A | ValueError: session_id 形态非法: 'source' |
| 160 | test_itemized_migration_integrity.py::test_raw_quarantine_is_complete_private_and_reported[tool_set_contribution] | A | ValueError: session_id 形态非法: 'source' |
| 161 | test_itemized_migration_integrity.py::test_raw_quarantine_is_complete_private_and_reported[request_context] | A | ValueError: session_id 形态非法: 'source' |
| 162 | test_itemized_migration_integrity.py::test_raw_quarantine_is_complete_private_and_reported[provider_metadata] | A | ValueError: session_id 形态非法: 'source' |
| 163 | test_itemized_migration_integrity.py::test_full_copy_lossless_gate_preserves_overlay_raw_and_refuses_install | A | ValueError: session_id 形态非法: 'source' |
| 164 | test_itemized_migration_integrity.py::test_corrupt_staging_is_not_initialized_as_recovered_target[offset] | A | ValueError: session_id 形态非法: 'source' |
| 165 | test_itemized_migration_integrity.py::test_corrupt_staging_is_not_initialized_as_recovered_target[final] | A | ValueError: session_id 形态非法: 'source' |
| 166 | test_itemized_migration_integrity.py::test_corrupt_staging_is_not_initialized_as_recovered_target[view] | A | ValueError: session_id 形态非法: 'source' |
| 167 | test_itemized_migration_semantics.py::test_tool_calls_and_results_get_target_local_linked_identity[success-completed-success-tool] | A | ValueError: session_id 形态非法: 'source' |
| 168 | test_itemized_migration_semantics.py::test_tool_calls_and_results_get_target_local_linked_identity[success-completed-success-function] | A | ValueError: session_id 形态非法: 'source' |
| 169 | test_itemized_migration_semantics.py::test_tool_calls_and_results_get_target_local_linked_identity[error-failed-unknown-tool] | A | ValueError: session_id 形态非法: 'source' |
| 170 | test_itemized_migration_semantics.py::test_tool_calls_and_results_get_target_local_linked_identity[error-failed-unknown-function] | A | ValueError: session_id 形态非法: 'source' |
| 171 | test_itemized_migration_semantics.py::test_tool_calls_and_results_get_target_local_linked_identity[None-unknown-unknown-tool] | A | ValueError: session_id 形态非法: 'source' |
| 172 | test_itemized_migration_semantics.py::test_tool_calls_and_results_get_target_local_linked_identity[None-unknown-unknown-function] | A | ValueError: session_id 形态非法: 'source' |
| 173 | test_itemized_migration_semantics.py::test_conflicting_candidates_never_create_turns[different_ids-legacy_turn_group_ambiguous] | A | ValueError: session_id 形态非法: 'source' |
| 174 | test_itemized_migration_semantics.py::test_conflicting_candidates_never_create_turns[multiple_roots-legacy_multiple_user_messages] | A | ValueError: session_id 形态非法: 'source' |
| 175 | test_itemized_migration_semantics.py::test_conflicting_candidates_never_create_turns[duplicate_message_id-legacy_identity_conflict] | A | ValueError: session_id 形态非法: 'source' |
| 176 | test_itemized_migration_semantics.py::test_conflicting_candidates_never_create_turns[unsupported_same_id-legacy_multiple_user_messages] | A | ValueError: session_id 形态非法: 'source' |
| 177 | test_itemized_migration_semantics.py::test_system_reminder_never_becomes_a_turn_member[True] | A | ValueError: session_id 形态非法: 'source' |
| 178 | test_itemized_migration_semantics.py::test_system_reminder_never_becomes_a_turn_member[False] | A | ValueError: session_id 形态非法: 'source' |
| 179 | test_itemized_migration_semantics.py::test_manifest_final_pointer_is_validated[False] | A | ValueError: session_id 形态非法: 'source' |
| 180 | test_itemized_migration_semantics.py::test_manifest_final_pointer_is_validated[True] | A | ValueError: session_id 形态非法: 'source' |
| 181 | test_itemized_migration_semantics.py::test_legacy_manifest_content_identity_is_checked[None] | A | ValueError: session_id 形态非法: 'source' |
| 182 | test_itemized_migration_semantics.py::test_legacy_manifest_content_identity_is_checked[hash] | A | ValueError: session_id 形态非法: 'source' |
| 183 | test_itemized_migration_semantics.py::test_legacy_manifest_content_identity_is_checked[length] | A | ValueError: session_id 形态非法: 'source' |
| 184 | test_itemized_migration_semantics.py::test_legacy_manifest_content_identity_is_checked[role] | A | ValueError: session_id 形态非法: 'source' |
| 185 | test_rollout_context_semantics.py::test_context_view_filters_control_records_and_keeps_business_messages | A | ValueError: session_id 形态非法: 'session_1' |
| 186 | test_rollout_context_semantics.py::test_incremental_checkpoint_keeps_tool_call_with_later_tool_result | A | ValueError: session_id 形态非法: 'session_1' |
| 187 | test_rollout_context_semantics.py::test_parallel_tool_continuation_restores_all_call_declarations | A | ValueError: session_id 形态非法: 'session_1' |
| 188 | test_rollout_context_semantics.py::test_context_view_validation_accepts_single_message_range | A | ValueError: session_id 形态非法: 'session_1' |
| 189 | test_rollout_context_semantics.py::test_compaction_control_event_keeps_message_cutoff_identity | A | ValueError: session_id 形态非法: 'session_1' |
| 190 | test_rollout_context_semantics.py::test_turn_anchor_reports_unreachable_when_no_complete_view_remains | A | ValueError: session_id 形态非法: 'session_1' |
| 191 | test_rollout_context_semantics.py::test_turn_anchor_walks_active_lineage_after_message_level_compaction | A | ValueError: session_id 形态非法: 'session_1' |
| 192 | test_rollout_context_semantics.py::test_logical_pruning_marks_unreferenced_checkpoints_without_touching_jsonl | A | ValueError: session_id 形态非法: 'session_1' |
| 193 | test_rollout_context_semantics.py::test_context_view_jump_validation_rejects_cycle_or_wrong_ancestor | A | ValueError: session_id 形态非法: 'session_1' |
| 194 | test_rollout_context_semantics.py::test_pruning_preserves_all_canonical_bytes_and_item_offsets | A | ValueError: session_id 形态非法: 'session_1' |
| 195 | test_rollout_fork_migration.py::test_full_copy_rejects_v1_without_migration_side_effects | A | ValueError: session_id 形态非法: 'source' |
| 196 | test_rollout_fork_migration.py::test_explicit_v1_import_then_v2_full_copy_keeps_tool_lineage_local | A | ValueError: session_id 形态非法: 'source' |
| 197 | test_rollout_fork_modes.py::test_all_fork_modes_materialize_independent_rollouts | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 198 | test_rollout_fork_modes.py::test_full_copy_localizes_detail_and_source_overlay_lineage | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 199 | test_rollout_fork_modes.py::test_history_replay_reuses_turn_root_without_execution | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 200 | test_rollout_fork_modes.py::test_stale_fork_anchor_is_rejected_before_target_creation | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 201 | test_rollout_fork_modes.py::test_full_copy_without_selector_keeps_complete_history_after_restart | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 202 | test_rollout_fork_modes.py::test_interrupted_fork_materialization_is_rolled_back_on_next_open | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 203 | test_rollout_fork_modes.py::test_context_fork_anchor_selects_completed_target_turn | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 204 | test_rollout_fork_modes.py::test_default_fork_does_not_materialize_running_tail | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 205 | test_rollout_fork_modes.py::test_fork_rejects_explicit_running_turn | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 206 | test_rollout_fork_modes.py::test_context_fork_turn_id_resolves_latest_active_lineage_view | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 207 | test_rollout_fork_modes.py::test_non_default_fork_modes_honor_turn_id_boundary[history_prefix_fork] | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 208 | test_rollout_fork_modes.py::test_non_default_fork_modes_honor_turn_id_boundary[full_rollout_copy] | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 209 | test_rollout_fork_modes.py::test_pinned_fork_keeps_source_deletion_blocked | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 210 | test_rollout_fork_modes.py::test_pinned_fork_retention_released_when_child_is_deleted | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 211 | test_rollout_fork_modes.py::test_context_and_history_preflight_rejects_before_target_creation | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 212 | test_rollout_fork_modes.py::test_full_copy_cancelled_historical_is_not_resumable_but_new_replay_is | B/workspace_id | RuntimeError: 注册会话时 manifest workspace_id 与分配不一致: expected=9a8db86e-3fea-4aff-a058-e6d011ff28fa, actual='00000000-0000-4000-8000-000000000001' |
| 213 | test_rollout_storage_benchmark.py::test_long_session_persists_message_deltas_without_snapshot_growth | A | ValueError: session_id 形态非法: 'session_1' |
| 214 | test_session_generation_strategies.py::test_new_per_run_is_idempotent_and_creates_cataloged_session | 既有债务 | AssertionError: assert ('2026', '09'...c8beeaa75526') == ('2026', '09'...123fa2e9fdd3') |
| 215 | test_session_generation_strategies.py::test_fork_new_and_report_back_creates_child_with_origin | 既有债务 | AssertionError: {"detail":"fork_source_not_completed: source 没有可复制的已完成 Turn"} |


### 直接迁移与调用链闭合

1. 本轮不修改 `session_bundle_factory`，不绕过 `validate_session_id`，删除未使用的新映射 helper 后，所有实际 ID 调用方直接采用仓库已有 canonical 测试值。
2. A 类实际边界按创建和读取调用链确认，不沿用原正则得出的互斥分类。`test_context_plan_registry.py` 的 `other`、`test_schema_v4_upgrade.py` 的 `other-session` 是冲突注入，实际主会话由随机非法前缀生成，归 C；`test_schema_v3_artifact_upgrade.py` 用字符串拼接生成随机 ID，亦归 C。本轮保留三者，防止误改负向语义。
3. 原正则漏掉只使用 factory 位置参数的 `test_rollout_context_semantics.py`、`test_rollout_storage_benchmark.py`、`test_schema_v3_protected_artifacts.py`，它们实际属于固定 ID 的 A 链路，纳入迁移。
4. 共享 `protected_source` fixture 的两个直接消费者 `test_rollout_fork_plan_registry.py`、`test_rollout_fork_reader_locks.py` 同步真实定位 ID，包含 `_WRITER` 子进程源码。否则上游已 canonical、下游仍读取 `source` 会引入 legacy 回归；这里没有改其断言或独立 B 业务逻辑。
5. `test_prepared_detail_verification.py`、`schema_v3_protected_helpers.py` 同步 protected upgrade 的固定会话 ID；`itemized_migration_helpers.migration_audits` 同步目标定位。无新增 helper 或兼容层。
6. 保留字典 `source` 字段名、消息正文/展示文字、工具参数名、非法 ID/foreign-owner 等冲突值。子进程里的会话参数与父进程同步，不修改进程退出、锁和故障注入断言。

### 冻结 protected 向量的身份迁移

原 schema2 AES-GCM 向量的 AAD 和已认证明文均携带 `upgrade-session`，单改目录或目标 ref 会破坏身份认证。离线使用独立 `cryptography.AESGCM` 认证原始固定密文，断言解密后唯一更改的 JSON 字段是 `session_id`，将 AAD 同字段同步为现有 canonical 值，再使用原测试 key/nonce 重封为新的静态固定字节。测试执行时仍使用字面量冻结密文，不调用当前产品 writer 生成期望。

正文、content hash、session digest、source revision、时间、旧 wire schema、nonce 和测试 key 均不变；注释明确新向量是 R21 身份迁移的结果，不再声称新密文是历史进程原样产物。独立审计数据见 `artifacts/r21-protected-vector-audit.json`。

`uv run pytest .../test_protected_detail_upgrade.py .../test_prepared_detail_verification.py .../test_schema_v3_protected_artifacts.py -q --tb=short`：130 passed（52.51s）；四个触及 Python 文件 Ruff 通过。

## 最终双模式验证与 fork import 7F 归因（收尾轮补记）

> 本节数字为 2026-09-19 冻结提交 fc7ce28 上的实测结果，取代此前引用的
> r24-finalize-* 数字（那一轮 catalog 全量运行时工作树仍在变动，仅作过程证据）。
> 本轮的两次全量运行期间 app/ 与 tests/integration/backend 无文件改动（mtime 复核）。

### 双模式全量数字（冻结提交 fc7ce28）

| 模式 | 结果 | 日志 |
| --- | --- | --- |
| 默认 catalog | 10 failed / 1149 passed / 0 errors（1082.86s） | artifacts/r21-integration-catalog-after.log |
| legacy（BOXTEAM_SESSION_CATALOG_RESOLVER=0） | 9 failed / 1150 passed / 0 errors（1097.64s） | artifacts/r21-integration-legacy.log |

两份日志经 cp 副本双读 + md5 一致裁定（catalog md5 `f0a52c64f83d546215b9a2c04ea7c03f`、
legacy md5 `5052fdde76dd2b903968161f1a7d158e`）。

### 215F 基线 → 残差的 delta 归因

基线：215 failed / 52 passed / 892 errors（artifacts/r21-integration-catalog-before.log）。
归因表见上文「215F 逐项归因」；下表按该表分桶汇总。

| 分桶 | 基线 | 残差 | 结论 |
| --- | --- | --- | --- |
| A 固定非法会话 ID | 181 | 0 | 全部收敛：调用方改为 canonical ID/`create_prefixed_id` |
| B/workspace_id 工作区身份不一致 | 16 | 0 | 全部收敛（test_rollout_fork_modes.py） |
| C 随机前缀非法 ID | 15 | 0 | 全部收敛（test_itemized_migration_legacy.py） |
| 既有债务 | 3 | 3 | 逐项一致（config_migration 1 + session_generation_strategies 2） |
| 新增：fork import 家族 | 0 | 7 | 见下节归因：既有深层债务，非本轮迁移引入 |
| ERROR 家族 | 892 | 0 | A/B/C 三类的 setup 错误随根因消失 |

基线 215F 中 181+16+15=212 项已收敛；残差 10F = 3 项登记债务 + 7 项 fork import 家族。
名义归属：A/B/C 三类测试文件适配均落在提交 7a50fef 内（R21 自身的 app/core 生产变更
独立进入 23f6860，不含 integration 测试文件）。

### fork import 7 failed 归因：既有深层债务，非 R21 迁移回归

test_rollout_fork_import_sources.py 的 7 failed（错误形如
`RuntimeError: full_rollout_copy message 没有对应 canonical item`）与 session ID
形态无关，三条独立证据：

1. **时间线证据**：R19 legacy 基线（artifacts/r19-integration-legacy.log，2026-09-16）
   已列出完全相同的 7 个 nodeid；该基线早于 ID canonical 化提交 7a50fef（2026-09-19），
   当时该文件仍使用 `"source"/"target"/"grandchild"` 别名字面量。
2. **ID 形态对照**：catalog 模式 + canonical ID 得 7 failed
   （artifacts/r21-a-final.log）；恢复原始 source/target ID 并以 legacy 模式运行同样 7 failed，
   错误逐字相同（artifacts/r21-fork-import-legacy-original-ids.log）。
3. **代码级根因**：`fork/remap_finalize.py:161` 要求 `messages` 表的每行能在
   `new_items` 中按 `metadata["projection_message_id"] == message_id` 找到对应 canonical item；
   而 full-copy 物化的 fork item 其 `projection_message_id` 为 `null`
   （artifacts/r21-fork-debug-dump.json），映射在 ID 重写前即已缺失。

补充口径：这 7 项在 catalog 基线中并非 FAILED 而是 setup ERROR（非 canonical ID 令
fixture 建不出来）；迁移让 setup 成功、测试真正执行后才暴露上述 remap 缺口，
即「ERROR → FAILED」是遮蔽解除而非新引入。legacy 模式下它们自 R19 起就是 FAILED。

结论：fork full-copy remap 链路在把 source item 复制为 target item 后，message 与
canonical item 的对应关系存在与 ID 形态无关的既有缺口。该家族自 R19 起登记为既有债务；
R21 不越权改生产 remap 代码，7F 保留并移交 fork remap 专项轮。

### 派生发现：静态 fixture 工作区现代化（移交后续轮）

tests/fixtures/workspaces/custom_tool_test_workspace 在收尾轮全量运行中被
SQLite 侧车文件污染：sessions/ses_b1a2c3d4e5f6478899aabbccddeeff03/rollout/ 与
ses_9f4e2c7a1b6d4830a5e8f2c1d7b90436/rollout/ 出现未跟踪的 `index.sqlite-wal`/`-shm`，
导致 test_real_rollout_fixture.py::test_custom_tool_fixture_asset_contract 断言
`rollout/` 目录内容集合时多出两项（该轮得 11F）。

- 定性：环境/模板污染，不是代码或本轮迁移的回归。把两个侧车文件移出模板后，
  该文件定向复跑 3 passed；随后冻结提交上的全量复跑恢复 10F，未再出现该失败。
- 侧车文件已移出，模板恢复为「index.sqlite + rollout.jsonl」两文件形态
  （`git status tests/fixtures` 为空）。
- 遗留风险：模板的真实布局现代化（日期桶 + catalog 权威）与 rollout v1→v2 仍待处理；
  其中 ses_4c0a…2345 等 v1 源同时被 migration machinery 测试消费，方案必须区分
  「v1 只读源」与「v2 runtime 消费」两类会话。此前记录的现象仍然成立：
  catalog 模式下 web 集成测试因模板携带旧 JSON 权威索引而启动失败
  （r21-boundary-head.log）；布局迁入日期桶后读取 turn projection 报
  `schema-upgrade-required: current=1, target=4`（r21-boundary-diag.log），
  需经 legacy_import_v1_to_v2 显式迁移或按当前产品 writer 重新生成。

### 报告恢复记录（收尾轮）

收尾轮发现 rounds/R21-impl.md 曾被截断为 82 行：标题被改写为
`# R21：catalog resolver 读取已发布 child thread`，本文前半的
「R21 integration catalog 测试迁移」整节（含 215 行逐项归因表、直接迁移口径、
冻结 protected 向量身份迁移）全部丢失，仅存于本次读取时的磁盘快照
（artifacts/r21-finish-read/R21-impl.md，md5 `6241b3ca8165555ed228964b7dd6fb3e`）。
本轮以该快照为准恢复全文，并保留收尾轮补记章节；恢复后的报告 md5 见交付回报。

恢复口径：全文以该快照为正文基线（299 行 / 46711 字节 / md5
`6241b3ca8165555ed228964b7dd6fb3e`），在其上追加本节；仅把「基线」小节中
「逐项归因待完成」一句改为指向本节，其余原样保留。
