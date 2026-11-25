ALTER TABLE message_projections RENAME TO message_projections_v29;
CREATE TABLE IF NOT EXISTS message_projections (
    message_sequence INTEGER PRIMARY KEY, text_preview TEXT, visible_text TEXT,
    response_outline_json TEXT NOT NULL,
    visible_text_length INTEGER NOT NULL, visible_text_truncated INTEGER NOT NULL DEFAULT 0,
    has_reasoning INTEGER NOT NULL, has_encrypted_reasoning INTEGER NOT NULL,
    has_tool_calls INTEGER NOT NULL, phase TEXT, projection_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
