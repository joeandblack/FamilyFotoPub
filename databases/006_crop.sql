ALTER TABLE media_adjustments ADD COLUMN IF NOT EXISTS crop jsonb;
INSERT INTO schema_versions(version) VALUES (6) ON CONFLICT DO NOTHING;
