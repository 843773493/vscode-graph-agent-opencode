-- 普通组合按 typed overlay binding 筛选，避免为未消费的旧来源逐行解析 JSON。
-- NULL 分支定位普通来源；活动 overlay 的 contribution_id 仍由既有主键定位。
CREATE INDEX IF NOT EXISTS context_contributions_overlay_index
ON context_contributions(json_extract(binding_manifest_json, '$.source_binding.overlay_id'));
