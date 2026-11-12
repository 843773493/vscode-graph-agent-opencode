ALTER TABLE fork_source_captures ADD COLUMN source_point_json TEXT;

UPDATE fork_source_captures
SET source_point_json = frozen_fork_point(request_json, preimage_hash, snapshot_sha256, marker_sha256)
WHERE json_extract(request_json, '$.source_view_id') IS NOT NULL
  AND json_extract(request_json, '$.source_view_revision') IS NOT NULL;

PRAGMA user_version = 11;
