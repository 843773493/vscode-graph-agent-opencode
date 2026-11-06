-- 普通组合只定位活动 overlay，并沿既有 epoch、创建时刻和 ID 顺序读取。
-- 仅索引活动记录，不为读取遍历已替代或已物化的历史。
CREATE INDEX IF NOT EXISTS source_overlays_active_index
ON source_overlays(checkpoint_ns, source_overlay_epoch, created_at, overlay_id)
WHERE status = 'active';
-- 幂等查重按完整 owner/namespace/key 定位；冲突仍由原 writer 明确报告。
CREATE INDEX IF NOT EXISTS source_overlays_idempotency_index
ON source_overlays(session_id, thread_id, checkpoint_ns, idempotency_key);
