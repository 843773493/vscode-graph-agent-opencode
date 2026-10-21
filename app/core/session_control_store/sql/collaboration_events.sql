CREATE TABLE IF NOT EXISTS collaboration_events (
        event_id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES collaboration_boards(team_id),
        ledger_revision INTEGER NOT NULL UNIQUE, event_json TEXT NOT NULL)
