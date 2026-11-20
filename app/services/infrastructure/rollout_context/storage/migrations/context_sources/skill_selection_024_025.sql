-- 旧封存内容不变；零表示尚未使用显式选择次序，由原封存来源确定首次选择基准。
ALTER TABLE context_source_control_states ADD COLUMN selection_sequence INTEGER NOT NULL DEFAULT 0 CHECK(selection_sequence >= 0);
ALTER TABLE context_source_control_states ADD COLUMN latest_visible_selection_sequence INTEGER NOT NULL DEFAULT 0 CHECK(latest_visible_selection_sequence >= 0 AND latest_visible_selection_sequence <= selection_sequence);
ALTER TABLE checkpoint_context_source_states ADD COLUMN selection_sequence INTEGER NOT NULL DEFAULT 0 CHECK(selection_sequence >= 0);
ALTER TABLE checkpoint_context_source_states ADD COLUMN latest_visible_selection_sequence INTEGER NOT NULL DEFAULT 0 CHECK(latest_visible_selection_sequence >= 0 AND latest_visible_selection_sequence <= selection_sequence);
