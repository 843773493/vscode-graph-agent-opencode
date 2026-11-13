CREATE TABLE resource_records (
    resource_id TEXT PRIMARY KEY NOT NULL,
    payload TEXT NOT NULL CHECK (json_type(payload) = 'object'
        AND json_extract(payload, '$.resource_id') = resource_id)
);
CREATE TABLE resource_leases (
    lease_id TEXT PRIMARY KEY NOT NULL,
    payload TEXT NOT NULL CHECK (json_type(payload) = 'object'
        AND json_extract(payload, '$.lease_id') = lease_id),
    resource_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.resource_id')) STORED NOT NULL
        REFERENCES resource_records(resource_id)
);
