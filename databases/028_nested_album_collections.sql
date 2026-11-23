DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=28) THEN
ALTER TABLE album_collections ADD COLUMN parent_id bigint
    REFERENCES album_collections(id) ON DELETE SET NULL;
ALTER TABLE album_collections ADD CONSTRAINT collection_not_own_parent
    CHECK (parent_id IS DISTINCT FROM id);
CREATE INDEX album_collection_parent_idx ON album_collections(parent_id);
INSERT INTO schema_versions(version) VALUES (28);
END IF;
END $$;
