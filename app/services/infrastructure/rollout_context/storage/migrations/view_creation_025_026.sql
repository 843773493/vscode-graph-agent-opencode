-- 旧 view 从未重写 rowid；按库中已有的插入顺序冻结序号，不按时间或随机 ID 猜测。
ALTER TABLE context_views ADD COLUMN creation_sequence INTEGER NOT NULL DEFAULT 0 CHECK(creation_sequence >= 0);
UPDATE context_views SET creation_sequence = rowid;
