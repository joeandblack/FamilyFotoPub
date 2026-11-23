ALTER TABLE media ADD COLUMN IF NOT EXISTS stored_sha256 text;
ALTER TABLE media ADD COLUMN IF NOT EXISTS original_relative_path text;
ALTER TABLE media ADD COLUMN IF NOT EXISTS original_size bigint;
ALTER TABLE media ADD COLUMN IF NOT EXISTS conversion_state text;
ALTER TABLE media ADD COLUMN IF NOT EXISTS conversion_error text;
INSERT INTO schema_versions(version) VALUES (3) ON CONFLICT DO NOTHING;
