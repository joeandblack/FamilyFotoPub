CREATE TABLE IF NOT EXISTS media_trash (
 media_id bigint PRIMARY KEY REFERENCES media(id),
 trashed_at timestamptz NOT NULL DEFAULT now(),
 deleting_at timestamptz,
 deleted_at timestamptz
);
INSERT INTO schema_versions(version) VALUES (4) ON CONFLICT DO NOTHING;
