ALTER TABLE jobs ADD COLUMN IF NOT EXISTS auto_trash_duplicates boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS photo_duplicate_trash (
 media_id bigint PRIMARY KEY REFERENCES media(id),
 job_id bigint NOT NULL REFERENCES jobs(id),
 preferred_id bigint NOT NULL REFERENCES media(id),
 version text NOT NULL, pixel_hash text NOT NULL, reason text NOT NULL,
 trashed_at timestamptz NOT NULL DEFAULT now(), restored_at timestamptz
);
CREATE INDEX IF NOT EXISTS photo_duplicate_trash_job ON photo_duplicate_trash(job_id,trashed_at);
INSERT INTO schema_versions(version) VALUES(22) ON CONFLICT DO NOTHING;
