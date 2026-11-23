ALTER TABLE jobs ADD COLUMN IF NOT EXISTS duplicates_status jsonb;
CREATE TABLE IF NOT EXISTS photo_fingerprints (
 media_id bigint PRIMARY KEY REFERENCES media(id), version text NOT NULL,
 stored_sha256 text NOT NULL, pixel_hash text NOT NULL, image_hash text NOT NULL,
 evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS photo_fingerprint_match ON photo_fingerprints(version,pixel_hash);
CREATE TABLE IF NOT EXISTS photo_duplicate_items (
 job_id bigint REFERENCES jobs(id), media_id bigint REFERENCES media(id),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','warning')),
 error text, PRIMARY KEY(job_id,media_id)
);
CREATE TABLE IF NOT EXISTS photo_duplicate_decisions (
 version text NOT NULL, pixel_hash text NOT NULL,
 decision text NOT NULL CHECK(decision IN ('candidate','confirmed','separate')),
 preferred_id bigint REFERENCES media(id), member_ids jsonb NOT NULL,
 revision integer NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(version,pixel_hash)
);
CREATE INDEX IF NOT EXISTS photo_duplicate_queue ON photo_duplicate_items(job_id,state,media_id);
INSERT INTO schema_versions(version) VALUES(20) ON CONFLICT DO NOTHING;
