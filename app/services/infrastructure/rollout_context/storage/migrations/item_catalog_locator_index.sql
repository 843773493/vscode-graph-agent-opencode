-- 消息恢复逐条按冻结 locator 反查 item；索引避免每条消息扫描全部历史。
CREATE INDEX IF NOT EXISTS item_catalog_locator_index ON item_catalog(jsonl_offset, jsonl_length);
