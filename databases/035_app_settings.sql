CREATE TABLE IF NOT EXISTS app_settings (
 key text PRIMARY KEY, value jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(), updated_by text NOT NULL
);
INSERT INTO schema_versions(version) VALUES (35) ON CONFLICT DO NOTHING;
