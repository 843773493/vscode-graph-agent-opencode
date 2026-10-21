
CREATE TABLE IF NOT EXISTS attachment_blobs (
    blob_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    relative_locator TEXT NOT NULL UNIQUE,
    length INTEGER NOT NULL CHECK (length >= 0),
    mime_type TEXT,
    protection TEXT NOT NULL DEFAULT 'private',
    availability TEXT NOT NULL CHECK (availability IN ('available', 'tombstoned')),
    tombstoned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
