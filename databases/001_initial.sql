CREATE TABLE IF NOT EXISTS schema_versions (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS jobs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_root text NOT NULL UNIQUE, state text NOT NULL DEFAULT 'scanning',
 started_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 total_bytes bigint NOT NULL DEFAULT 0, error text, inventory_complete boolean NOT NULL DEFAULT false
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS inventory_complete boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS media (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 sha256 text NOT NULL UNIQUE, relative_path text NOT NULL UNIQUE,
 name text NOT NULL, kind text NOT NULL CHECK (kind IN ('image','video')),
 size bigint NOT NULL, captured_at timestamp, width integer, height integer,
 created_at timestamptz NOT NULL DEFAULT now(), metadata_error text
);
CREATE TABLE IF NOT EXISTS import_items (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 job_id bigint NOT NULL REFERENCES jobs(id), source_path text NOT NULL,
 size bigint NOT NULL, mtime_ns bigint NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','imported','duplicate','unsupported','error')),
 media_id bigint REFERENCES media(id), error text, UNIQUE(job_id,source_path)
);
CREATE TABLE IF NOT EXISTS job_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 job_id bigint NOT NULL REFERENCES jobs(id), created_at timestamptz NOT NULL DEFAULT now(),
 event text NOT NULL, details text
);
CREATE INDEX IF NOT EXISTS media_captured_idx ON media(captured_at DESC NULLS LAST,id DESC);
CREATE INDEX IF NOT EXISTS import_queue_idx ON import_items(job_id,state,id);
CREATE INDEX IF NOT EXISTS import_media_idx ON import_items(media_id);
INSERT INTO schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;
