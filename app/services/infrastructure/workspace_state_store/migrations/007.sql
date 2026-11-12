
    ALTER TABLE config_pending_candidate ADD COLUMN candidate_ref TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS config_pending_candidate_ref_unique
        ON config_pending_candidate(candidate_ref)
        WHERE candidate_ref IS NOT NULL;
