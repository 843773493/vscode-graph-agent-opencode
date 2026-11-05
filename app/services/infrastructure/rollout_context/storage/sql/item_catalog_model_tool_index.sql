-- 按 ModelCall 定位工具声明及结果复用候选，不扫描其他调用的历史正文。
CREATE INDEX IF NOT EXISTS item_catalog_model_tool_index
ON item_catalog(json_extract(projection_manifest_json,'$.projection_identity.model_call_id'), semantic_kind, tool_call_id, item_sequence);
