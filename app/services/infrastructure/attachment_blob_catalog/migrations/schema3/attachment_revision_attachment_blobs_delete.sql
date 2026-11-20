CREATE TRIGGER IF NOT EXISTS attachment_revision_attachment_blobs_delete AFTER DELETE ON attachment_blobs BEGIN UPDATE attachment_catalog_revision SET revision = revision + 1 WHERE id = 1; END
