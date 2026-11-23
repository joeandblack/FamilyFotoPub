DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=26) THEN
ALTER TABLE albums ADD COLUMN sort_order bigint;
INSERT INTO schema_versions(version) VALUES (26);
END IF;
END $$;
