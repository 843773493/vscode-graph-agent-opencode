
CREATE TABLE IF NOT EXISTS remote_observations (
    observation_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('source','target')),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    snapshot_json TEXT CHECK(snapshot_json IS NULL OR json_valid(snapshot_json)),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(observation_id,side)
)
