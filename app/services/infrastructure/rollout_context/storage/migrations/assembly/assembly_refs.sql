
CREATE TABLE IF NOT EXISTS assembly_item_refs (
    assembly_id TEXT NOT NULL,
    ref_ordinal INTEGER NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    ref_thread_id TEXT NOT NULL CHECK(length(ref_thread_id) > 0),
    semantic_kind TEXT,
    payload_kind TEXT,
    status TEXT,
    content_hash TEXT,
    source_revision TEXT,
    content_length INTEGER,
    detail_ref TEXT,
    contribution_id TEXT,
    visibility TEXT NOT NULL DEFAULT 'internal',
    protection TEXT NOT NULL DEFAULT 'public',
    availability TEXT NOT NULL DEFAULT 'available',
    PRIMARY KEY(assembly_id, ref_ordinal),
    UNIQUE(assembly_id, ref_type, ref_id, ref_thread_id)
);
CREATE TABLE IF NOT EXISTS context_assembly_selections (
    assembly_id TEXT NOT NULL,
    plan_ordinal INTEGER NOT NULL,
    selection_kind TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    ref_thread_id TEXT NOT NULL CHECK(length(ref_thread_id) > 0),
    included INTEGER NOT NULL,
    omission_reason TEXT,
    loss_json TEXT NOT NULL DEFAULT '[]',
    source_revision TEXT,
    content_length INTEGER,
    content_hash TEXT,
    visibility TEXT NOT NULL,
    protection TEXT NOT NULL,
    availability TEXT NOT NULL,
    base_delta_role TEXT NOT NULL,
    source_overlay_epoch INTEGER,
    overlay_from_revision TEXT,
    overlay_to_revision TEXT,
    overlay_diff_hash TEXT,
    detail_ref TEXT,
    contribution_id TEXT,
    contribution_ordinal INTEGER,
    source_cut_position_kind TEXT,
    source_cut_item_id TEXT,
    CHECK (
        (source_cut_position_kind IS NULL AND source_cut_item_id IS NULL)
        OR (source_cut_position_kind IS 'start' AND source_cut_item_id IS NULL
            AND included = 1 AND ref_type = 'request_only')
        OR (source_cut_position_kind IS 'after_canonical'
            AND source_cut_item_id IS NOT NULL AND length(source_cut_item_id) > 0
            AND included = 1 AND ref_type = 'request_only')
    ),
    PRIMARY KEY(assembly_id, plan_ordinal),
    UNIQUE(assembly_id, ref_type, ref_id, ref_thread_id)
);
CREATE INDEX IF NOT EXISTS assembly_item_refs_item_index ON assembly_item_refs(ref_type, ref_id, ref_thread_id);
CREATE INDEX IF NOT EXISTS assembly_item_refs_ordinal_index ON assembly_item_refs(assembly_id, ref_ordinal);
CREATE INDEX IF NOT EXISTS assembly_item_refs_contribution_index ON assembly_item_refs(assembly_id, contribution_id);
CREATE INDEX IF NOT EXISTS assembly_item_refs_detail_index ON assembly_item_refs(assembly_id, detail_ref);
CREATE INDEX IF NOT EXISTS context_assembly_selections_ref_index ON context_assembly_selections(ref_type, ref_id, ref_thread_id);
CREATE INDEX IF NOT EXISTS context_assembly_selections_contribution_index ON context_assembly_selections(assembly_id, contribution_id);
CREATE UNIQUE INDEX IF NOT EXISTS context_assembly_selections_contribution_unique ON context_assembly_selections(assembly_id, contribution_id) WHERE contribution_id IS NOT NULL;
