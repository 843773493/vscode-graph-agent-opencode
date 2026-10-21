
        CREATE TABLE IF NOT EXISTS database_meta (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1), rollout_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL, thread_id TEXT NOT NULL, schema_version INTEGER NOT NULL,
            message_format_version INTEGER NOT NULL, database_state TEXT NOT NULL,
            last_commit_id INTEGER, last_message_sequence INTEGER NOT NULL,
            last_control_sequence INTEGER NOT NULL, committed_jsonl_offset INTEGER NOT NULL,
            active_branch_id TEXT, projection_epoch INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            rollout_format_version INTEGER NOT NULL DEFAULT 2,
            last_item_sequence INTEGER NOT NULL DEFAULT 0,
            history_view_revision INTEGER NOT NULL DEFAULT 0,
            source_overlay_epoch INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id INTEGER PRIMARY KEY AUTOINCREMENT, from_version INTEGER NOT NULL,
            to_version INTEGER NOT NULL, migration_name TEXT NOT NULL,
            migration_checksum TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, completed_at TEXT, error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS storage_commits (
            commit_id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL UNIQUE,
            first_message_sequence INTEGER, last_message_sequence INTEGER,
            jsonl_start_offset INTEGER NOT NULL, jsonl_end_offset INTEGER NOT NULL,
            first_control_sequence INTEGER, last_control_sequence INTEGER,
            jsonl_fsync_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, committed_at TEXT,
            commit_kind TEXT NOT NULL DEFAULT 'item_convergence',
            commit_mode TEXT NOT NULL DEFAULT 'item_bearing',
            jsonl_offset_before INTEGER NOT NULL DEFAULT 0,
            jsonl_offset_after INTEGER NOT NULL DEFAULT 0,
            jsonl_record_count INTEGER NOT NULL DEFAULT 0,
            subject_id TEXT,
            idempotency_key TEXT,
            outcome TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS control_events (
            control_sequence INTEGER PRIMARY KEY AUTOINCREMENT, control_id TEXT NOT NULL UNIQUE,
            control_kind TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            branch_id TEXT, view_id TEXT, checkpoint_id TEXT, payload_json TEXT NOT NULL,
            transaction_id TEXT NOT NULL, previous_event_hash TEXT, event_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS branches (
            branch_id TEXT PRIMARY KEY, branch_kind TEXT NOT NULL, status TEXT NOT NULL,
            head_view_id TEXT, head_checkpoint_id TEXT, parent_branch_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checkpoint_namespace_state (
            checkpoint_ns TEXT PRIMARY KEY, active_branch_id TEXT NOT NULL,
            projection_epoch INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_views (
            view_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, parent_view_id TEXT,
            view_kind TEXT NOT NULL, head_turn_id TEXT, head_message_sequence INTEGER NOT NULL,
            logical_turn_count INTEGER NOT NULL, control_sequence INTEGER, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_view_ranges (
            view_id TEXT NOT NULL, range_index INTEGER NOT NULL, source_kind TEXT NOT NULL,
            source_view_id TEXT, start_message_sequence INTEGER, end_message_sequence INTEGER,
            source_start_turn_ordinal INTEGER, source_end_turn_ordinal INTEGER,
            logical_start_turn_ordinal INTEGER, logical_end_turn_ordinal INTEGER,
            range_ordinal INTEGER, source_start_ordinal INTEGER, source_end_ordinal INTEGER,
            message_start_sequence INTEGER, message_end_sequence INTEGER,
            logical_start_ordinal INTEGER, logical_end_ordinal INTEGER,
            PRIMARY KEY(view_id, range_index)
        );
        CREATE TABLE IF NOT EXISTS context_view_jumps (
            view_id TEXT NOT NULL, jump_level INTEGER NOT NULL,
            ancestor_view_id TEXT NOT NULL, ancestor_depth INTEGER NOT NULL,
            PRIMARY KEY(view_id, jump_level)
        );
        CREATE TABLE IF NOT EXISTS message_projections (
            message_sequence INTEGER PRIMARY KEY, text_preview TEXT, visible_text TEXT,
            visible_text_length INTEGER NOT NULL, visible_text_truncated INTEGER NOT NULL DEFAULT 0,
            has_reasoning INTEGER NOT NULL, has_encrypted_reasoning INTEGER NOT NULL,
            has_tool_calls INTEGER NOT NULL, phase TEXT, projection_version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY, turn_ordinal INTEGER NOT NULL UNIQUE,
            turn_kind TEXT NOT NULL, branch_id TEXT NOT NULL,
            first_message_sequence INTEGER NOT NULL, last_message_sequence INTEGER NOT NULL,
            user_message_sequence INTEGER, final_message_sequence INTEGER,
            final_message_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_view_turns (
            view_id TEXT NOT NULL, turn_id TEXT NOT NULL, logical_turn_ordinal INTEGER NOT NULL,
            user_message_sequence INTEGER, final_message_sequence INTEGER,
            root_input_item_id TEXT, fork_lineage_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(view_id, turn_id), UNIQUE(view_id, logical_turn_ordinal)
        );
        CREATE TABLE IF NOT EXISTS tool_calls (
            tool_call_id TEXT NOT NULL, assistant_message_sequence INTEGER NOT NULL,
            call_index INTEGER NOT NULL, tool_name TEXT NOT NULL, status TEXT NOT NULL,
            result_message_sequence INTEGER, argument_length INTEGER NOT NULL,
            result_length INTEGER, argument_hash TEXT, result_hash TEXT, summary_text TEXT,
            started_at TEXT, completed_at TEXT, projection_version INTEGER NOT NULL
            , PRIMARY KEY(tool_call_id, assistant_message_sequence)
        );
        CREATE TABLE IF NOT EXISTS reasoning_blocks (
            message_sequence INTEGER NOT NULL,
            content_block_index INTEGER NOT NULL,
            item_index INTEGER NOT NULL DEFAULT 0,
            carrier_type TEXT NOT NULL,
            item_id TEXT,
            reasoning_text TEXT,
            summary_text TEXT,
            signature_present INTEGER NOT NULL DEFAULT 0,
            encrypted_length INTEGER,
            encrypted_hash TEXT,
            provider_id TEXT,
            projection_version INTEGER NOT NULL,
            PRIMARY KEY(message_sequence, content_block_index, item_index)
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY, checkpoint_ns TEXT NOT NULL, commit_id INTEGER NOT NULL,
            message_sequence INTEGER NOT NULL, message_count INTEGER NOT NULL,
            parent_checkpoint_id TEXT, view_id TEXT NOT NULL, branch_id TEXT NOT NULL,
            checkpoint_version INTEGER NOT NULL, checkpoint_timestamp TEXT NOT NULL,
            checkpoint_kind TEXT NOT NULL DEFAULT 'normal', status TEXT NOT NULL DEFAULT 'active',
            checkpoint_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
            envelope_serializer_name TEXT NOT NULL DEFAULT 'msgpack',
            versions_seen_type TEXT NOT NULL, versions_seen_blob BLOB NOT NULL,
            versions_seen_length INTEGER NOT NULL, versions_seen_hash TEXT NOT NULL,
            pending_sends_type TEXT NOT NULL, pending_sends_blob BLOB NOT NULL,
            pending_sends_length INTEGER NOT NULL, pending_sends_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_writes (
            checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, task_path TEXT NOT NULL,
            write_index INTEGER NOT NULL, channel TEXT NOT NULL, serializer_name TEXT NOT NULL,
            value_blob BLOB NOT NULL, value_length INTEGER NOT NULL, value_hash TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(checkpoint_id, task_id, task_path, write_index)
        );
        CREATE TABLE IF NOT EXISTS fork_origins (
            fork_id TEXT PRIMARY KEY, child_session_id TEXT NOT NULL,
            source_session_id TEXT NOT NULL, source_checkpoint_id TEXT,
            source_view_id TEXT, fork_mode TEXT NOT NULL, relationship TEXT NOT NULL,
            copied_message_count INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retention_refs (
            retention_id TEXT PRIMARY KEY, reference_kind TEXT NOT NULL, reference_id TEXT NOT NULL,
            target_view_id TEXT, target_message_sequence INTEGER, owner_session_id TEXT,
            expires_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fork_materializations (
            materialization_id TEXT PRIMARY KEY, fork_id TEXT NOT NULL UNIQUE,
            target_session_id TEXT NOT NULL, source_session_id TEXT NOT NULL,
            source_checkpoint_id TEXT, source_view_id TEXT, fork_mode TEXT NOT NULL,
            relationship TEXT NOT NULL, status TEXT NOT NULL,
            rollback_jsonl_offset INTEGER NOT NULL,
            copied_message_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT, created_at TEXT NOT NULL,
            target_committed_at TEXT, committed_at TEXT,
            source_thread_id TEXT NOT NULL, target_thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL, source_snapshot_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_catalog (
            item_sequence INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL UNIQUE,
            semantic_kind TEXT NOT NULL,
            payload_kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('completed','partial','incomplete','cancelled','failed','unknown')),
            turn_id TEXT,
            execution_id TEXT,
            turn_scope TEXT,
            message_group_id TEXT,
            wire_role TEXT,
            producer_ref_json TEXT NOT NULL,
            payload_length INTEGER NOT NULL DEFAULT 0,
            source_revision TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            jsonl_offset INTEGER NOT NULL,
            jsonl_length INTEGER NOT NULL,
            commit_id INTEGER,
            operation_anchor_capable INTEGER NOT NULL DEFAULT 0,
            searchable INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            tool_call_id TEXT,
            projection_manifest_json TEXT NOT NULL CHECK(
                json_valid(projection_manifest_json)
                AND json_type(projection_manifest_json, '$.extensions') IS 'object'
                AND json_type(projection_manifest_json, '$.projection_group') IS NOT NULL
                AND json_type(projection_manifest_json, '$.projection_group') IN ('object','null')
                AND json_type(projection_manifest_json, '$.projection_identity') IS NOT NULL
                AND json_type(projection_manifest_json, '$.projection_identity') IN ('object','null')
                AND json_type(projection_manifest_json, '$.tool_result_evidence') IS NOT NULL
                AND json_type(projection_manifest_json, '$.tool_result_evidence') IN ('object','null')
                AND json_type(projection_manifest_json, '$.source_manifest') IS NOT NULL
                AND json_type(projection_manifest_json, '$.source_manifest') IN ('object','null')
                AND json_type(projection_manifest_json, '$.provenance_relations') IS 'array'
            )
        );
        CREATE TABLE IF NOT EXISTS item_projections (
            item_sequence INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL UNIQUE,
            id TEXT NOT NULL UNIQUE,
            semantic_kind TEXT NOT NULL,
            payload_kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('completed','partial','incomplete','cancelled','failed','unknown')),
            turn_id TEXT,
            turn_scope TEXT,
            message_group_id TEXT,
            wire_role TEXT,
            content TEXT NOT NULL,
            content_length INTEGER NOT NULL,
            content_truncated INTEGER NOT NULL DEFAULT 0,
            has_reasoning INTEGER NOT NULL DEFAULT 0,
            has_tool_calls INTEGER NOT NULL DEFAULT 0,
            phase TEXT,
            content_hash TEXT NOT NULL,
            projection_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS item_relations (
            relation_id TEXT PRIMARY KEY,
            relation TEXT NOT NULL,
            canonical_item_id TEXT,
            canonical_relation_ordinal INTEGER,
            source_ref TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            attempt INTEGER,
            supersedes_relation_id TEXT,
            replay_input INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            CHECK ((canonical_item_id IS NULL AND canonical_relation_ordinal IS NULL)
                OR (typeof(canonical_item_id) = 'text'
                    AND typeof(canonical_relation_ordinal) = 'integer'
                    AND canonical_relation_ordinal >= 0)),
            UNIQUE(canonical_item_id, canonical_relation_ordinal),
            UNIQUE(relation, source_ref, target_ref, attempt)
        );
        CREATE TABLE IF NOT EXISTS item_parts (
            item_id TEXT NOT NULL,
            part_id TEXT NOT NULL,
            part_ordinal INTEGER NOT NULL,
            part_semantic_kind TEXT NOT NULL,
            content_prefix_hash TEXT,
            content_hash TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            line_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(item_id, part_id),
            UNIQUE(item_id, part_ordinal)
        );
        CREATE TABLE IF NOT EXISTS operation_anchors (
            anchor_id TEXT PRIMARY KEY,
            anchor_kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            part_id TEXT,
            mode TEXT NOT NULL CHECK(mode IN ('before','inclusive')),
            view_id TEXT,
            branch_id TEXT,
            recovery_capability TEXT NOT NULL,
            fragment_identity TEXT,
            fragment_length INTEGER,
            fragment_hash TEXT,
            fragment_layout_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(item_id, part_id, mode, view_id, branch_id)
        );
        CREATE TABLE IF NOT EXISTS turn_acceptances (
            accepted_ingress_id TEXT PRIMARY KEY,
            acceptance_idempotency_key TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL UNIQUE,
            payload_hash TEXT NOT NULL,
            source_session_id TEXT,
            source_accepted_ingress_id TEXT,
            source_acceptance_idempotency_key TEXT,
            identity_origin TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, thread_id, accepted_ingress_id),
            UNIQUE(session_id, thread_id, acceptance_idempotency_key),
            UNIQUE(session_id, thread_id, turn_id)
        );

        CREATE TABLE IF NOT EXISTS fork_debug_snapshots (
            materialization_id TEXT PRIMARY KEY REFERENCES fork_materializations(materialization_id),
            target_session_id TEXT NOT NULL,
            target_thread_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','ready','published')),
            proof_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ready_at TEXT,
            published_at TEXT,
            CHECK((state='prepared' AND ready_at IS NULL AND published_at IS NULL)
                OR (state='ready' AND ready_at IS NOT NULL AND published_at IS NULL)
                OR (state='published' AND ready_at IS NOT NULL AND published_at IS NOT NULL))
        );
        CREATE TABLE IF NOT EXISTS turn_records (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            turn_ordinal INTEGER NOT NULL UNIQUE,
            source_branch_id TEXT NOT NULL,
            root_input_item_id TEXT NOT NULL UNIQUE,
            root_input_item_sequence INTEGER,
            accepted_ingress_id TEXT NOT NULL UNIQUE,
            acceptance_idempotency_key TEXT NOT NULL UNIQUE,
            initial_execution_id TEXT NOT NULL UNIQUE,
            last_execution_id TEXT,
            status TEXT NOT NULL,
            final_item_id TEXT,
            replay_of_turn_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(session_id, thread_id, turn_id)
        );
        CREATE TABLE IF NOT EXISTS executions (
            execution_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            turn_id TEXT,
            execution_origin TEXT NOT NULL CHECK(execution_origin IN ('user_turn','internal')),
            job_id TEXT NOT NULL CHECK(length(job_id) > 0),
            admission_idempotency_key TEXT NOT NULL CHECK(length(admission_idempotency_key) > 0),
            admission_payload_hash TEXT NOT NULL CHECK(
                length(admission_payload_hash) = 78
                AND substr(admission_payload_hash, 1, 14) = 'sha256:jcs:v1:'
            ),
            attempt INTEGER NOT NULL,
            execution_ordinal INTEGER NOT NULL DEFAULT 1,
            accepted_ingress_id TEXT,
            outcome TEXT NOT NULL,
            resumed_from_execution_id TEXT,
            replay_of_execution_id TEXT,
            first_model_call_id TEXT,
            last_model_call_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (execution_origin = 'user_turn' AND turn_id IS NOT NULL AND accepted_ingress_id IS NOT NULL)
                OR
                (execution_origin = 'internal' AND turn_id IS NULL AND accepted_ingress_id IS NULL)
            ),
            UNIQUE(session_id, thread_id, turn_id, attempt),
            UNIQUE(session_id, thread_id, execution_id),
            UNIQUE(session_id, thread_id, admission_idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS model_calls (
            model_call_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            attempt_ordinal INTEGER NOT NULL DEFAULT 1,
            provider TEXT NOT NULL,
            provider_request_id TEXT,
            retry_of_model_call_id TEXT,
            assembly_id TEXT,
            dispatch_state TEXT NOT NULL DEFAULT 'ready',
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, thread_id, execution_id, attempt),
            UNIQUE(session_id, thread_id, model_call_id)
        );
        CREATE TABLE IF NOT EXISTS turn_execution_links (
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            execution_role TEXT NOT NULL,
            execution_ordinal INTEGER NOT NULL DEFAULT 1,
            link_idempotency_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(session_id, thread_id, turn_id, execution_id),
            UNIQUE(session_id, thread_id, turn_id, link_idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS context_assemblies (
            assembly_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            turn_id TEXT,
            execution_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            history_view_revision INTEGER NOT NULL,
            source_overlay_epoch INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            model_call_id TEXT,
            status TEXT NOT NULL,
            outcome TEXT,
            detail_ref TEXT,
            created_at TEXT NOT NULL,
            sealed_at TEXT,
            terminal_at TEXT,
            UNIQUE(session_id, thread_id, assembly_id)
        );
        CREATE TABLE IF NOT EXISTS context_assembly_contributions (
            assembly_id TEXT NOT NULL,
            contribution_ordinal INTEGER NOT NULL,
            contribution_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_hash TEXT,
            content_length INTEGER,
            request_only INTEGER NOT NULL,
            contribution_kind TEXT NOT NULL DEFAULT 'prompt',
            visibility TEXT NOT NULL DEFAULT 'internal',
            protection TEXT NOT NULL DEFAULT 'public',
            binding_manifest_json TEXT NOT NULL CHECK(
                json_valid(binding_manifest_json)
                AND json_type(binding_manifest_json, '$.extensions') IS 'object'
                AND json_type(binding_manifest_json, '$.source_binding') IS NOT NULL
                AND json_type(binding_manifest_json, '$.source_binding') IN ('object', 'null')
                AND json_type(binding_manifest_json, '$.root_placement') IS 'text'
                AND json_extract(binding_manifest_json, '$.root_placement') IN ('root_eligible', 'tail_only')
            ),
            CHECK(content_hash IS NOT NULL),
            PRIMARY KEY(assembly_id, contribution_ordinal),
            UNIQUE(assembly_id, contribution_id)
        );
        CREATE TABLE IF NOT EXISTS context_contributions (
            contribution_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_hash TEXT,
            content_length INTEGER,
            request_only INTEGER NOT NULL,
            contribution_kind TEXT NOT NULL DEFAULT 'prompt',
            visibility TEXT NOT NULL DEFAULT 'internal',
            protection TEXT NOT NULL DEFAULT 'public',
            assembly_id TEXT,
            contribution_ordinal INTEGER,
            source_ordinal INTEGER NOT NULL CHECK(typeof(source_ordinal) = 'integer' AND source_ordinal >= 0),
            selection_role TEXT NOT NULL DEFAULT 'direct' CHECK(selection_role IN ('direct','backing_only')),
            replacement_policy TEXT NOT NULL DEFAULT 'immutable' CHECK(replacement_policy IN ('immutable','replaceable')),
            binding_manifest_json TEXT NOT NULL CHECK(
                json_valid(binding_manifest_json)
                AND json_type(binding_manifest_json, '$.extensions') IS 'object'
                AND json_type(binding_manifest_json, '$.source_binding') IS NOT NULL
                AND json_type(binding_manifest_json, '$.source_binding') IN ('object', 'null')
                AND json_type(binding_manifest_json, '$.root_placement') IS 'text'
                AND json_extract(binding_manifest_json, '$.root_placement') IN ('root_eligible', 'tail_only')
            ),
            created_at TEXT NOT NULL,
            CHECK(content_hash IS NOT NULL)
        );
        CREATE TABLE IF NOT EXISTS source_overlays (
            overlay_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            source_overlay_epoch INTEGER NOT NULL,
            base_ref TEXT,
            delta_ref TEXT,
            base_source_revision TEXT,
            base_content_length INTEGER,
            base_content_hash TEXT,
            delta_source_revision TEXT,
            delta_content_length INTEGER,
            delta_content_hash TEXT,
            delta_from_revision TEXT,
            delta_to_revision TEXT,
            delta_diff_hash TEXT,
            supersedes_overlay_id TEXT,
            materializes_overlay_id TEXT,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_plan_details (
            detail_ref TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            assembly_id TEXT NOT NULL,
            detail_id TEXT NOT NULL,
            detail_kind TEXT NOT NULL,
            retention_class TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('public','internal','private')),
            relative_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_length INTEGER NOT NULL CHECK(content_length >= 0),
                        protection TEXT NOT NULL DEFAULT 'public',
            availability TEXT NOT NULL DEFAULT 'available',
            required INTEGER NOT NULL,
            sensitive INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            UNIQUE(session_id, thread_id, assembly_id, detail_id),
            CHECK(json_valid(detail_ref)
                AND json_type(detail_ref, '$.session_id') IS 'text'
                AND json_type(detail_ref, '$.thread_id') IS 'text'
                AND json_type(detail_ref, '$.assembly_id') IS 'text'
                AND json_type(detail_ref, '$.detail_id') IS 'text'
                AND json_extract(detail_ref, '$.session_id') = session_id
                AND json_extract(detail_ref, '$.thread_id') = thread_id
                AND json_extract(detail_ref, '$.assembly_id') = assembly_id
                AND json_extract(detail_ref, '$.detail_id') = detail_id)
        );
        CREATE TABLE IF NOT EXISTS legacy_migration_reports (
            migration_id TEXT PRIMARY KEY,
            source_session_id TEXT NOT NULL,
            target_session_id TEXT,
            source_format_version INTEGER NOT NULL,
            target_format_version INTEGER NOT NULL,
            status TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS turns_ordinal_index ON turns(turn_ordinal);
        CREATE INDEX IF NOT EXISTS context_view_turns_ordinal_index ON context_view_turns(view_id, logical_turn_ordinal);
        CREATE INDEX IF NOT EXISTS context_view_turns_turn_index ON context_view_turns(turn_id, view_id);
        CREATE INDEX IF NOT EXISTS tool_calls_id_index ON tool_calls(tool_call_id, assistant_message_sequence);
        CREATE INDEX IF NOT EXISTS tool_calls_result_sequence_index ON tool_calls(result_message_sequence);
        CREATE INDEX IF NOT EXISTS checkpoint_commit_index ON checkpoints(commit_id DESC);
        CREATE INDEX IF NOT EXISTS checkpoint_channels_view_index ON checkpoint_channels(context_view_id);
        CREATE INDEX IF NOT EXISTS pending_writes_checkpoint_index ON pending_writes(checkpoint_id, task_path, write_index);
        CREATE INDEX IF NOT EXISTS fork_materializations_status_index ON fork_materializations(status, created_at);
        CREATE INDEX IF NOT EXISTS item_catalog_turn_index ON item_catalog(turn_id, item_sequence);
        CREATE INDEX IF NOT EXISTS item_catalog_turn_scope_index ON item_catalog(turn_id, turn_scope, item_sequence);
        CREATE INDEX IF NOT EXISTS item_catalog_group_index ON item_catalog(message_group_id, item_sequence);
        CREATE INDEX IF NOT EXISTS item_catalog_semantic_index ON item_catalog(semantic_kind, item_sequence);
        CREATE INDEX IF NOT EXISTS item_catalog_status_index ON item_catalog(status, item_sequence);
        CREATE INDEX IF NOT EXISTS item_catalog_commit_index ON item_catalog(commit_id, item_sequence);
        CREATE INDEX IF NOT EXISTS item_catalog_operation_index ON item_catalog(operation_anchor_capable, item_sequence);
        CREATE INDEX IF NOT EXISTS item_projections_turn_index ON item_projections(turn_id, item_sequence);
        CREATE INDEX IF NOT EXISTS item_projections_semantic_index ON item_projections(semantic_kind, item_sequence);
        CREATE INDEX IF NOT EXISTS item_projections_status_index ON item_projections(status, item_sequence);
        CREATE INDEX IF NOT EXISTS item_parts_hash_index ON item_parts(content_hash);
        CREATE INDEX IF NOT EXISTS executions_turn_index ON executions(turn_id, attempt);
        CREATE INDEX IF NOT EXISTS model_calls_execution_index ON model_calls(execution_id, attempt);
        CREATE INDEX IF NOT EXISTS executions_owner_index ON executions(session_id, thread_id, turn_id, attempt);
        CREATE INDEX IF NOT EXISTS executions_internal_history_index
            ON executions(session_id, thread_id, execution_origin, created_at);
        CREATE INDEX IF NOT EXISTS item_catalog_execution_index ON item_catalog(execution_id, item_sequence);
        CREATE INDEX IF NOT EXISTS model_calls_owner_index ON model_calls(session_id, thread_id, execution_id, attempt);
        CREATE INDEX IF NOT EXISTS turn_execution_links_owner_index ON turn_execution_links(session_id, thread_id, turn_id, execution_id);
        CREATE INDEX IF NOT EXISTS turn_records_owner_index ON turn_records(session_id, thread_id, turn_ordinal);
        CREATE INDEX IF NOT EXISTS turn_acceptances_owner_index ON turn_acceptances(session_id, thread_id, accepted_ingress_id);
        CREATE INDEX IF NOT EXISTS turn_records_ordinal_index ON turn_records(turn_ordinal);
        CREATE INDEX IF NOT EXISTS turn_records_ingress_index ON turn_records(accepted_ingress_id);
        CREATE INDEX IF NOT EXISTS context_assemblies_turn_index ON context_assemblies(turn_id, created_at);
        CREATE INDEX IF NOT EXISTS context_assemblies_owner_index ON context_assemblies(session_id, thread_id, turn_id, execution_id);
        CREATE INDEX IF NOT EXISTS context_plan_details_owner_index ON context_plan_details(session_id, thread_id, assembly_id);
        CREATE INDEX IF NOT EXISTS context_assembly_contributions_source_index ON context_assembly_contributions(source_kind, source_revision);
        CREATE INDEX IF NOT EXISTS tool_set_snapshots_assembly_index ON tool_set_snapshots(assembly_id, tool_set_snapshot_id);
        CREATE INDEX IF NOT EXISTS context_assembly_contributions_ordinal_index ON context_assembly_contributions(assembly_id, contribution_ordinal);
        CREATE INDEX IF NOT EXISTS source_overlays_epoch_index ON source_overlays(session_id, thread_id, source_overlay_epoch);
        CREATE UNIQUE INDEX IF NOT EXISTS context_contributions_source_ordinal_unique
            ON context_contributions(source_ordinal)
            WHERE source_ordinal IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS context_contributions_assembly_ordinal_unique
            ON context_contributions(assembly_id, contribution_ordinal)
            WHERE assembly_id IS NOT NULL AND contribution_ordinal IS NOT NULL;
