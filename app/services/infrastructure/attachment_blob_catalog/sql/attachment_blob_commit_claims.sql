
CREATE TABLE IF NOT EXISTS attachment_blob_commit_claims (
    digest TEXT PRIMARY KEY,
    blob_id TEXT NOT NULL UNIQUE,
    final_relative_locator TEXT NOT NULL UNIQUE,
    expected_length INTEGER NOT NULL CHECK (expected_length >= 0),
    first_claim_utc_date TEXT NOT NULL,
    winning_ingest_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('claimed', 'published', 'aborted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
