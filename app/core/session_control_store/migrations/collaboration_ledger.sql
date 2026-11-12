
CREATE TABLE IF NOT EXISTS collaboration_ledger (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL
)
