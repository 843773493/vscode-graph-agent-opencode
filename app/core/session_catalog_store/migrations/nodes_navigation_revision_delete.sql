
    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_delete
    AFTER DELETE ON nodes
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END
