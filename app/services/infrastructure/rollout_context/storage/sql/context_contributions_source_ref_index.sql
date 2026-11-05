-- 状态输入按冻结来源身份及版本定位，不逐个解析全部历史请求的贡献 manifest。
CREATE INDEX IF NOT EXISTS context_contributions_source_ref_index
ON context_assembly_contributions(json_extract(binding_manifest_json, '$.source_binding.source_ref'), source_revision);
