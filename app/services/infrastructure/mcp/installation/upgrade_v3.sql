ALTER TABLE operation_owners RENAME TO previous_operation_owners;

CREATE TABLE operation_owners (
    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
    pid INTEGER NOT NULL,
    started REAL NOT NULL,
    execution_payload TEXT NOT NULL
);

INSERT INTO operation_owners
SELECT previous.operation_id, previous.pid, previous.started, operations.payload
FROM previous_operation_owners AS previous
JOIN operations ON operations.operation_id = previous.operation_id;

DROP TABLE previous_operation_owners;

PRAGMA user_version = 4;
