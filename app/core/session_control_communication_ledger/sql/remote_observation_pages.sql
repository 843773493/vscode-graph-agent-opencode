
CREATE TABLE IF NOT EXISTS remote_observation_pages (
    observation_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('source','target')),
    page_identity TEXT NOT NULL,
    page_ordinal INTEGER NOT NULL CHECK(page_ordinal >= 0),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    target_result_json TEXT CHECK(target_result_json IS NULL OR json_valid(target_result_json)),
    PRIMARY KEY(observation_id,side,page_ordinal),
    UNIQUE(side,page_identity),
    FOREIGN KEY(observation_id,side) REFERENCES remote_observations(observation_id,side)
        ON DELETE CASCADE
)
