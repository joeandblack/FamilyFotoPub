-- Additive: existing paths and processing states remain untouched.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_storage jsonb;
INSERT INTO schema_versions(version) VALUES (9) ON CONFLICT DO NOTHING;
