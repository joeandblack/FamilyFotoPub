ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'server';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_label text;
CREATE TABLE IF NOT EXISTS upload_sessions (
 id uuid PRIMARY KEY, owner_name text NOT NULL, request_key uuid NOT NULL,
 manifest_hash text NOT NULL, label text NOT NULL, options jsonb NOT NULL,
 state text NOT NULL DEFAULT 'uploading', total_bytes bigint NOT NULL,
 job_id bigint UNIQUE REFERENCES jobs(id), error text, cancel_requested boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 cleaned_at timestamptz, UNIQUE(owner_name,request_key)
);
CREATE TABLE IF NOT EXISTS upload_files (
 session_id uuid NOT NULL REFERENCES upload_sessions(id), file_id integer NOT NULL,
 relative_path text NOT NULL, size bigint NOT NULL, modified_ms bigint NOT NULL,
 received bigint NOT NULL DEFAULT 0, sha256 text, verified boolean NOT NULL DEFAULT false,
 disposition text NOT NULL, PRIMARY KEY(session_id,file_id), UNIQUE(session_id,relative_path)
);
CREATE INDEX IF NOT EXISTS upload_queue_idx ON upload_sessions(state,created_at);
INSERT INTO schema_versions(version) VALUES (11) ON CONFLICT DO NOTHING;
