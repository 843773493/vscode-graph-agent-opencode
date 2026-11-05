CREATE TABLE workspace_locators (
    workspace_id TEXT PRIMARY KEY,
    root TEXT NOT NULL UNIQUE
);

PRAGMA user_version = 3;
