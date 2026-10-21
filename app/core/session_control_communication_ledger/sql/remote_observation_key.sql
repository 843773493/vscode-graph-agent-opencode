
CREATE TABLE IF NOT EXISTS remote_observation_key (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    cursor_key BLOB NOT NULL CHECK(length(cursor_key)=32)
)
