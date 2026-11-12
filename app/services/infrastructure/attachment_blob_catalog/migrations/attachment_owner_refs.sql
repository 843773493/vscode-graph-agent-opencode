
CREATE TABLE IF NOT EXISTS attachment_owner_refs (
    owner_ref_id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL UNIQUE,
    blob_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    owner_session_id TEXT NOT NULL,
    owner_thread_id TEXT NOT NULL,
    file_name TEXT,
    mime_type TEXT,
    variant TEXT NOT NULL DEFAULT 'original',
    item_ref TEXT,
    retention_until TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'released')),
    released_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
