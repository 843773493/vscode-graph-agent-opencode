CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    installation_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_owners (
    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
    pid INTEGER NOT NULL,
    started REAL NOT NULL
);

PRAGMA user_version = 1;
