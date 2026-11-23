DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=33) THEN
ALTER TABLE albums ADD COLUMN cover_framing jsonb NOT NULL DEFAULT '{}';
INSERT INTO schema_versions(version) VALUES (33);
END IF;
END $$;
