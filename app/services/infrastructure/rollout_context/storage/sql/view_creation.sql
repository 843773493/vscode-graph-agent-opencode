-- 同一 SQLite 写事务内为新 view 分配顺序；已有 view 的尾部追加不修改创建顺序。
CREATE UNIQUE INDEX IF NOT EXISTS context_views_creation_sequence ON context_views(creation_sequence);
CREATE TRIGGER IF NOT EXISTS context_views_assign_creation_sequence AFTER INSERT ON context_views
WHEN NEW.creation_sequence = 0
BEGIN
    UPDATE context_views
    SET creation_sequence = (SELECT MAX(creation_sequence) + 1 FROM context_views)
    WHERE view_id = NEW.view_id;
END;
CREATE TRIGGER IF NOT EXISTS context_views_preserve_creation_sequence BEFORE UPDATE OF creation_sequence ON context_views
WHEN OLD.creation_sequence > 0 AND NEW.creation_sequence <> OLD.creation_sequence
BEGIN
    SELECT RAISE(ABORT, 'context view creation sequence is immutable');
END;
