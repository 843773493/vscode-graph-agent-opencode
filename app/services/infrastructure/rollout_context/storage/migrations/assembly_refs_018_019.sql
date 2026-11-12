
CREATE TEMP TABLE assembly_ref_migration_guard (
    valid INTEGER NOT NULL CHECK(valid = 1)
);
INSERT INTO assembly_ref_migration_guard
SELECT CASE WHEN
    json_valid(snapshot_json)
    AND json_type(snapshot_json, '$.refs') IS 'array'
    AND json_type(snapshot_json, '$.selection') IS 'array'
    AND json_extract(snapshot_json, '$.assembly_id') IS assembly_id
    AND json_extract(snapshot_json, '$.session_id') IS session_id
    AND json_extract(snapshot_json, '$.thread_id') IS thread_id
    AND json_extract(snapshot_json, '$.plan_id') IS plan_id
    AND (SELECT count(*) FROM assembly_item_refs r WHERE r.assembly_id = a.assembly_id)
        = json_array_length(snapshot_json, '$.refs')
    AND (SELECT count(*) FROM context_assembly_selections r WHERE r.assembly_id = a.assembly_id)
        = json_array_length(snapshot_json, '$.selection')
    THEN 1 ELSE 0 END FROM context_assemblies a;


INSERT INTO assembly_ref_migration_guard
SELECT CASE WHEN a.assembly_id IS NOT NULL
    AND typeof(r.ref_ordinal) = 'integer' AND r.ref_ordinal >= 0
    AND json_type(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']')) IS 'object'
    AND json_extract(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']'), '$.ref_type') IS r.ref_type
    AND json_extract(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']'), '$.ref_id') IS r.ref_id
    AND json_extract(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']'), '$.session_id') IS a.session_id
    AND json_type(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']'), '$.thread_id') IS 'text'
    AND length(json_extract(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']'), '$.thread_id')) > 0
    AND (r.ref_type = 'tool_set' OR EXISTS (
        SELECT 1 FROM context_plan_refs p
        WHERE p.session_id = a.session_id AND p.thread_id = a.thread_id
        AND p.plan_id = a.plan_id AND p.ref_type = r.ref_type AND p.ref_id = r.ref_id
        AND p.ref_thread_id IS json_extract(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']'), '$.thread_id')
        AND json_extract(p.ref_json, '$.ref_type') IS r.ref_type
        AND json_extract(p.ref_json, '$.ref_id') IS r.ref_id
        AND json_extract(p.ref_json, '$.thread_id') IS p.ref_thread_id
        AND json_extract(p.ref_json, '$.session_id') IS p.session_id
    ))
    THEN 1 ELSE 0 END
FROM assembly_item_refs r LEFT JOIN context_assemblies a ON a.assembly_id = r.assembly_id;

ALTER TABLE assembly_item_refs RENAME TO assembly_item_refs_schema18;
DROP INDEX assembly_item_refs_item_index;
DROP INDEX assembly_item_refs_ordinal_index;
DROP INDEX assembly_item_refs_contribution_index;
DROP INDEX assembly_item_refs_detail_index;
CREATE TABLE assembly_item_refs (
    assembly_id TEXT NOT NULL,
    ref_ordinal INTEGER NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    ref_thread_id TEXT NOT NULL CHECK(length(ref_thread_id) > 0),
    semantic_kind TEXT,
    payload_kind TEXT,
    status TEXT,
    content_hash TEXT,
    source_revision TEXT,
    content_length INTEGER,
    detail_ref TEXT,
    contribution_id TEXT,
    visibility TEXT NOT NULL DEFAULT 'internal',
    protection TEXT NOT NULL DEFAULT 'public',
    availability TEXT NOT NULL DEFAULT 'available',
    PRIMARY KEY(assembly_id, ref_ordinal),
    UNIQUE(assembly_id, ref_type, ref_id, ref_thread_id)
);
INSERT INTO assembly_item_refs (assembly_id,ref_ordinal,ref_type,ref_id,semantic_kind,payload_kind,status,content_hash,source_revision,content_length,detail_ref,contribution_id,visibility,protection,availability,ref_thread_id) SELECT r.assembly_id,r.ref_ordinal,r.ref_type,r.ref_id,r.semantic_kind,r.payload_kind,r.status,r.content_hash,r.source_revision,r.content_length,r.detail_ref,r.contribution_id,r.visibility,r.protection,r.availability,json_extract(json_extract(a.snapshot_json, '$.refs[' || r.ref_ordinal || ']'), '$.thread_id') FROM assembly_item_refs_schema18 r JOIN context_assemblies a ON a.assembly_id = r.assembly_id;
DROP TABLE assembly_item_refs_schema18;

INSERT INTO assembly_ref_migration_guard
SELECT CASE WHEN a.assembly_id IS NOT NULL
    AND typeof(r.plan_ordinal) = 'integer' AND r.plan_ordinal >= 0
    AND json_type(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref')) IS 'object'
    AND json_extract(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.ref_type') IS r.ref_type
    AND json_extract(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.ref_id') IS r.ref_id
    AND json_extract(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.session_id') IS a.session_id
    AND json_type(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.thread_id') IS 'text'
    AND length(json_extract(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.thread_id')) > 0
    AND (r.ref_type = 'tool_set' OR EXISTS (
        SELECT 1 FROM context_plan_refs p
        WHERE p.session_id = a.session_id AND p.thread_id = a.thread_id
        AND p.plan_id = a.plan_id AND p.ref_type = r.ref_type AND p.ref_id = r.ref_id
        AND p.ref_thread_id IS json_extract(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.thread_id')
        AND json_extract(p.ref_json, '$.ref_type') IS r.ref_type
        AND json_extract(p.ref_json, '$.ref_id') IS r.ref_id
        AND json_extract(p.ref_json, '$.thread_id') IS p.ref_thread_id
        AND json_extract(p.ref_json, '$.session_id') IS p.session_id
    ))
    THEN 1 ELSE 0 END
FROM context_assembly_selections r LEFT JOIN context_assemblies a ON a.assembly_id = r.assembly_id;


INSERT INTO assembly_ref_migration_guard
SELECT CASE WHEN json_type(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].plan_ordinal') IS 'integer'
    AND json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].plan_ordinal') IS r.plan_ordinal
    AND (r.ref_type != 'tool_set' OR EXISTS (
        SELECT 1 FROM tool_set_snapshots t WHERE t.session_id = a.session_id
        AND t.thread_id = a.thread_id AND t.plan_id = a.plan_id
        AND t.tool_set_snapshot_id = r.ref_id
        AND t.thread_id IS json_extract(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.thread_id')
    )) THEN 1 ELSE 0 END
FROM context_assembly_selections r JOIN context_assemblies a ON a.assembly_id = r.assembly_id;

ALTER TABLE context_assembly_selections RENAME TO context_assembly_selections_schema18;
DROP INDEX context_assembly_selections_ref_index;
DROP INDEX context_assembly_selections_contribution_index;
DROP INDEX context_assembly_selections_contribution_unique;
CREATE TABLE context_assembly_selections (
    assembly_id TEXT NOT NULL,
    plan_ordinal INTEGER NOT NULL,
    selection_kind TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    ref_thread_id TEXT NOT NULL CHECK(length(ref_thread_id) > 0),
    included INTEGER NOT NULL,
    omission_reason TEXT,
    loss_json TEXT NOT NULL DEFAULT '[]',
    source_revision TEXT,
    content_length INTEGER,
    content_hash TEXT,
    visibility TEXT NOT NULL,
    protection TEXT NOT NULL,
    availability TEXT NOT NULL,
    base_delta_role TEXT NOT NULL,
    source_overlay_epoch INTEGER,
    overlay_from_revision TEXT,
    overlay_to_revision TEXT,
    overlay_diff_hash TEXT,
    detail_ref TEXT,
    contribution_id TEXT,
    contribution_ordinal INTEGER,
    source_cut_position_kind TEXT,
    source_cut_item_id TEXT,
    CHECK (
        (source_cut_position_kind IS NULL AND source_cut_item_id IS NULL)
        OR (source_cut_position_kind IS 'start' AND source_cut_item_id IS NULL
            AND included = 1 AND ref_type = 'request_only')
        OR (source_cut_position_kind IS 'after_canonical'
            AND source_cut_item_id IS NOT NULL AND length(source_cut_item_id) > 0
            AND included = 1 AND ref_type = 'request_only')
    ),
    PRIMARY KEY(assembly_id, plan_ordinal),
    UNIQUE(assembly_id, ref_type, ref_id, ref_thread_id)
);
INSERT INTO context_assembly_selections (assembly_id,plan_ordinal,selection_kind,ref_type,ref_id,included,omission_reason,loss_json,source_revision,content_length,content_hash,visibility,protection,availability,base_delta_role,source_overlay_epoch,overlay_from_revision,overlay_to_revision,overlay_diff_hash,detail_ref,contribution_id,contribution_ordinal,source_cut_position_kind,source_cut_item_id,ref_thread_id) SELECT r.assembly_id,r.plan_ordinal,r.selection_kind,r.ref_type,r.ref_id,r.included,r.omission_reason,r.loss_json,r.source_revision,r.content_length,r.content_hash,r.visibility,r.protection,r.availability,r.base_delta_role,r.source_overlay_epoch,r.overlay_from_revision,r.overlay_to_revision,r.overlay_diff_hash,r.detail_ref,r.contribution_id,r.contribution_ordinal,r.source_cut_position_kind,r.source_cut_item_id,json_extract(json_extract(a.snapshot_json, '$.selection[' || r.plan_ordinal || '].ref'), '$.thread_id') FROM context_assembly_selections_schema18 r JOIN context_assemblies a ON a.assembly_id = r.assembly_id;
DROP TABLE context_assembly_selections_schema18;
CREATE INDEX IF NOT EXISTS assembly_item_refs_item_index ON assembly_item_refs(ref_type, ref_id, ref_thread_id);
CREATE INDEX IF NOT EXISTS assembly_item_refs_ordinal_index ON assembly_item_refs(assembly_id, ref_ordinal);
CREATE INDEX IF NOT EXISTS assembly_item_refs_contribution_index ON assembly_item_refs(assembly_id, contribution_id);
CREATE INDEX IF NOT EXISTS assembly_item_refs_detail_index ON assembly_item_refs(assembly_id, detail_ref);
CREATE INDEX IF NOT EXISTS context_assembly_selections_ref_index ON context_assembly_selections(ref_type, ref_id, ref_thread_id);
CREATE INDEX IF NOT EXISTS context_assembly_selections_contribution_index ON context_assembly_selections(assembly_id, contribution_id);
CREATE UNIQUE INDEX IF NOT EXISTS context_assembly_selections_contribution_unique ON context_assembly_selections(assembly_id, contribution_id) WHERE contribution_id IS NOT NULL;
DROP TABLE assembly_ref_migration_guard;