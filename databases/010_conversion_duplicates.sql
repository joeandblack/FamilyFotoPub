ALTER TABLE media ADD COLUMN IF NOT EXISTS conversion_duplicate_of bigint REFERENCES media(id);
ALTER TABLE media ADD COLUMN IF NOT EXISTS duplicate_output_sha256 text;
INSERT INTO schema_versions(version) VALUES (10) ON CONFLICT DO NOTHING;
