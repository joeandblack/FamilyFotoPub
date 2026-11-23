CREATE TABLE IF NOT EXISTS video_metadata (
 media_id bigint PRIMARY KEY REFERENCES media(id),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','warning','ancillary')),
 metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
 error text, verified_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_versions(version) VALUES (2) ON CONFLICT DO NOTHING;
