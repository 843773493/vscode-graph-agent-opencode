CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    artifact_key TEXT,
    producer_operation_id TEXT REFERENCES operations(operation_id),
    payload TEXT NOT NULL
);

CREATE INDEX operation_producers ON operations(producer_operation_id);

CREATE TABLE artifacts (
    installation_id TEXT PRIMARY KEY,
    artifact_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);

CREATE TABLE operation_owners (
    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
    pid INTEGER NOT NULL,
    started REAL NOT NULL
);

CREATE TABLE artifact_preparations (
    artifact_key TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    installation_id TEXT NOT NULL UNIQUE,
    generation TEXT NOT NULL
);

PRAGMA user_version = 2;
