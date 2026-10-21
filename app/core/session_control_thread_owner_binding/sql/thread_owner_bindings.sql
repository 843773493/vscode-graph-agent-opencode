
CREATE TABLE IF NOT EXISTS thread_owner_bindings (
    thread_id TEXT PRIMARY KEY,
    final_relative_locator TEXT NOT NULL DEFAULT '',
    prefix_epoch INTEGER NOT NULL DEFAULT 1 CHECK (prefix_epoch >= 1),
    prefix_epoch_reason TEXT NOT NULL DEFAULT 'initial' CHECK (
        prefix_epoch_reason IN ('initial', 'compaction', 'rewind',
        'toolset_changed')),
    stable_prefix_hash TEXT,
    stable_prefix_length INTEGER,
    resource_activation_snapshot_ref TEXT,
    assembly_parent_ref TEXT,
    mutation_provenance TEXT,
    attachment_refs TEXT NOT NULL DEFAULT '[]',
    variant_refs TEXT NOT NULL DEFAULT '[]',
    resource_refs TEXT NOT NULL DEFAULT '[]',
    source_revision_refs TEXT NOT NULL DEFAULT '[]',
    tracking_registrations TEXT NOT NULL DEFAULT '[]',
    selection_provenance TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
