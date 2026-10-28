-- 存储 owner 已持有库位置，候选记录不再重复保存真实路径。
ALTER TABLE config_pending_candidate DROP COLUMN persistence_location;
