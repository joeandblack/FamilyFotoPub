ALTER TABLE jobs ADD COLUMN IF NOT EXISTS auto_duplicates boolean NOT NULL DEFAULT false;
INSERT INTO schema_versions(version) VALUES (21) ON CONFLICT DO NOTHING;
