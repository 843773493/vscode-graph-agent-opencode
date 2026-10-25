
    ALTER TABLE config_pending_candidate ADD COLUMN secret_bindings_json TEXT
        NOT NULL DEFAULT '{}';
