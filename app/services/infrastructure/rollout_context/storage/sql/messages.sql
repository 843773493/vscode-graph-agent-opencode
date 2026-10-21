        CREATE TABLE IF NOT EXISTS messages (
            message_sequence INTEGER PRIMARY KEY, message_id TEXT NOT NULL,
            turn_id TEXT, execution_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
            jsonl_offset INTEGER NOT NULL, jsonl_length INTEGER NOT NULL,
            content_length INTEGER NOT NULL, content_hash TEXT NOT NULL,
            visibility TEXT NOT NULL, commit_id INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_turn_index ON messages(turn_id, message_sequence);
        CREATE INDEX IF NOT EXISTS messages_role_index ON messages(role, message_sequence);
        CREATE INDEX IF NOT EXISTS messages_offset_index ON messages(jsonl_offset);
        CREATE INDEX IF NOT EXISTS messages_execution_index ON messages(execution_id, message_sequence);
CREATE INDEX IF NOT EXISTS messages_logical_identity ON messages(message_id, message_sequence);
