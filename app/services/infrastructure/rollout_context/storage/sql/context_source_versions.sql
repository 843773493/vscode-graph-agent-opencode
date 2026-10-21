
CREATE TABLE IF NOT EXISTS checkpoint_context_source_snapshots (
    checkpoint_id TEXT PRIMARY KEY,
    checkpoint_ns TEXT NOT NULL,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_count INTEGER NOT NULL CHECK(source_count >= 0),
    source_assembly_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoint_context_source_states (
    checkpoint_id TEXT NOT NULL REFERENCES checkpoint_context_source_snapshots(checkpoint_id),
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    name TEXT NOT NULL,
    binding_revision TEXT,
    tracking_status TEXT NOT NULL CHECK(tracking_status IN ('tracked','untracked')),
    latest_visible_committed_revision TEXT,
    latest_revision TEXT,
    state_revision INTEGER NOT NULL CHECK(state_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(checkpoint_id, source_id)
);
CREATE TABLE IF NOT EXISTS context_epoch_transitions (
    transition_id TEXT PRIMARY KEY,
    checkpoint_ns TEXT NOT NULL,
    source_checkpoint_id TEXT,
    target_view_id TEXT NOT NULL REFERENCES context_views(view_id),
    reason TEXT NOT NULL CHECK(reason IN ('rewind','compaction','runtime_notice_consumed')),
    control_sequence INTEGER CHECK(control_sequence > 0),
    canonical_cut_kind TEXT CHECK(
        canonical_cut_kind IN ('complete','before_canonical','after_canonical')
    ),
    canonical_cut_item_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','consumed','superseded')),
    consumed_assembly_id TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    notice_consumption_acceptance_id INTEGER REFERENCES storage_commits(commit_id),
    notice_selection_hash TEXT,
    CHECK((status = 'consumed') = (consumed_assembly_id IS NOT NULL)),
    CHECK((notice_consumption_acceptance_id IS NULL) = (notice_selection_hash IS NULL)),
    CHECK(
        (reason = 'runtime_notice_consumed' AND source_checkpoint_id IS NULL
            AND control_sequence IS NULL AND canonical_cut_kind IS NULL
            AND canonical_cut_item_id IS NULL AND notice_consumption_acceptance_id IS NOT NULL)
        OR (reason IN ('rewind','compaction') AND source_checkpoint_id IS NOT NULL
            AND control_sequence IS NOT NULL AND canonical_cut_kind IS NOT NULL AND (
                (canonical_cut_kind = 'complete' AND canonical_cut_item_id IS NULL)
                OR (canonical_cut_kind IN ('before_canonical','after_canonical')
                    AND canonical_cut_item_id IS NOT NULL AND length(canonical_cut_item_id) > 0)))
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS context_epoch_pending_namespace
    ON context_epoch_transitions(checkpoint_ns, target_view_id)
    WHERE status = 'pending' AND reason IN ('rewind','compaction');
CREATE UNIQUE INDEX IF NOT EXISTS context_epoch_notice_acceptance
    ON context_epoch_transitions(notice_consumption_acceptance_id)
    WHERE notice_consumption_acceptance_id IS NOT NULL;
