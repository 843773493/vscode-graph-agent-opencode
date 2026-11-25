-- 仅保存冻结来源与生成结果；pending/generating、租约与错误正文仍归后台任务。
CREATE TABLE IF NOT EXISTS context_compaction_candidates (
    candidate_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    checkpoint_ns TEXT NOT NULL,
    source_view_id TEXT NOT NULL REFERENCES context_views(view_id),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    result_detail_ref TEXT REFERENCES context_plan_details(detail_ref),
    predecessor_result_detail_ref TEXT REFERENCES context_plan_details(detail_ref),
    applied_summary_item_id TEXT REFERENCES item_catalog(item_id),
    applied_view_id TEXT REFERENCES context_views(view_id),
    discarded_reason TEXT,
    apply_on_next_request INTEGER NOT NULL DEFAULT 0 CHECK(apply_on_next_request IN (0,1)),
    created_at TEXT NOT NULL,
    CHECK((applied_summary_item_id IS NULL) = (applied_view_id IS NULL)),
    CHECK(applied_view_id IS NULL OR (result_detail_ref IS NOT NULL AND discarded_reason IS NULL)),
    CHECK(discarded_reason IS NULL OR length(discarded_reason) > 0)
);
CREATE INDEX IF NOT EXISTS context_compaction_candidate_details
    ON context_compaction_candidates(result_detail_ref);
CREATE INDEX IF NOT EXISTS context_compaction_proposal_identity
    ON context_compaction_candidates(checkpoint_ns,json_extract(manifest_json,'$.model_config_hash'),
        json_extract(manifest_json,'$.target_constraint_hash'),created_at);
-- 采用事务增量登记行范围；只存原 Item 引用，不复制原文。pruning 后仍保留位置。
CREATE TABLE IF NOT EXISTS compaction_history_ranges (
    checkpoint_ns TEXT NOT NULL,
    item_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    ref_json TEXT NOT NULL CHECK(json_valid(ref_json)),
    render_version INTEGER NOT NULL CHECK(render_version=2),
    line_start INTEGER NOT NULL CHECK(line_start>=1),
    line_count INTEGER NOT NULL CHECK(line_count>=1),
    adopted_control_sequence INTEGER NOT NULL REFERENCES control_events(control_sequence),
    PRIMARY KEY(checkpoint_ns,item_id,source_revision),
    UNIQUE(checkpoint_ns,line_start)
);
-- 安全正文只保存原 JSONL 字节范围与 hash；literal 仅承载投影生成的类型／脱敏证据。
CREATE TABLE IF NOT EXISTS compaction_history_lines (
    checkpoint_ns TEXT NOT NULL,
    item_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    line_ordinal INTEGER NOT NULL CHECK(line_ordinal>=0),
    prefix TEXT NOT NULL,
    source_offset INTEGER,
    source_length INTEGER,
    source_hash TEXT,
    quoted INTEGER NOT NULL CHECK(quoted IN (0,1)),
    literal TEXT,
    CHECK((source_offset IS NULL AND source_length IS NULL AND source_hash IS NULL AND literal IS NOT NULL)
       OR (source_offset>=0 AND source_length>0 AND source_hash IS NOT NULL AND literal IS NULL)),
    PRIMARY KEY(checkpoint_ns,item_id,source_revision,line_ordinal),
    FOREIGN KEY(checkpoint_ns,item_id,source_revision)
        REFERENCES compaction_history_ranges(checkpoint_ns,item_id,source_revision)
);
-- 复用不可变 contribution 的直接来源，清理不扫描正文文件或全请求 JSON。
CREATE INDEX IF NOT EXISTS context_assembly_summary_result_index
ON context_assembly_contributions(json_extract(binding_manifest_json, '$.source_binding.summary_result_ref'));
