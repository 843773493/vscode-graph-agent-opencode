
    ALTER TABLE config_pending_candidate ADD COLUMN base_active_revision INTEGER;
    ALTER TABLE config_pending_candidate ADD COLUMN persistence_location TEXT
        NOT NULL DEFAULT '';
