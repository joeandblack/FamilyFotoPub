DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=31) THEN
ALTER TABLE album_collections ADD COLUMN sort_order bigint;
INSERT INTO schema_versions(version) VALUES (31);
END IF;
END $$;
