
CREATE TABLE IF NOT EXISTS item_resource_references (
    item_id TEXT NOT NULL REFERENCES item_catalog(item_id) ON DELETE CASCADE,
    reference_ordinal INTEGER NOT NULL CHECK(reference_ordinal >= 0),
    resource_kind TEXT NOT NULL,
    resource_uri TEXT NOT NULL,
    PRIMARY KEY(item_id, reference_ordinal)
);
CREATE INDEX IF NOT EXISTS item_resource_references_uri
    ON item_resource_references(resource_kind, resource_uri, item_id);
