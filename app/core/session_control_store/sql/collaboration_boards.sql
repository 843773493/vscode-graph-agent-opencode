CREATE TABLE IF NOT EXISTS collaboration_boards (
        team_id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version >= 1),
        board_json TEXT NOT NULL)
