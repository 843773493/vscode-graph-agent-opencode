
CREATE TABLE IF NOT EXISTS catalog_metadata (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    generation INTEGER NOT NULL,
    navigation_revision INTEGER NOT NULL
)
