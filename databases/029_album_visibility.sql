DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=29) THEN
ALTER TABLE albums ADD COLUMN show_on_albums boolean NOT NULL DEFAULT true;
INSERT INTO schema_versions(version) VALUES (29);
END IF;
END $$;
