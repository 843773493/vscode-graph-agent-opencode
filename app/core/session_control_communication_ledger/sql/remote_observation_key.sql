
CREATE TABLE IF NOT EXISTS remote_observation_key (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    root_key BLOB NOT NULL CHECK(length(root_key)=32),
    generation BLOB NOT NULL CHECK(length(generation)=16)
)
