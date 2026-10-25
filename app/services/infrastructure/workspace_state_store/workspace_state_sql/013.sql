
    ALTER TABLE config_source_layers DROP COLUMN source_path;
    ALTER TABLE config_source_layers DROP COLUMN backup_path;
    ALTER TABLE config_source_layers ADD COLUMN uri TEXT;
    ALTER TABLE config_source_journal DROP COLUMN source_path;
    ALTER TABLE config_source_journal ADD COLUMN uri TEXT;
