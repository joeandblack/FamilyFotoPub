-- Unknown historical and automated deletions remain administrator-only.
ALTER TABLE media_trash ADD COLUMN IF NOT EXISTS deleted_by text;
INSERT INTO schema_versions(version) VALUES (37) ON CONFLICT DO NOTHING;
