ALTER TABLE jobs ADD COLUMN IF NOT EXISTS auto_metadata boolean NOT NULL DEFAULT false;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS auto_conversion boolean NOT NULL DEFAULT false;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS metadata_status jsonb;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS conversion_status jsonb;
INSERT INTO schema_versions(version) VALUES (7) ON CONFLICT DO NOTHING;
