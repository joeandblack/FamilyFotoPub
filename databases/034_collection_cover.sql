DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=34) THEN
ALTER TABLE album_collections ADD COLUMN cover_media_id bigint REFERENCES media(id) ON DELETE SET NULL;
ALTER TABLE album_collections ADD COLUMN cover_framing jsonb NOT NULL DEFAULT '{}';
INSERT INTO schema_versions(version) VALUES (34);
END IF;
END $$;
