-- Derived data only: no changes to originals or manual album structures.
CREATE TABLE face_models (
 id text PRIMARY KEY, manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE face_runs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 model_id text NOT NULL REFERENCES face_models(id), actor text NOT NULL,
 state text NOT NULL DEFAULT 'queued', error text,
 heartbeat_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE face_preferences (
 media_id bigint PRIMARY KEY REFERENCES media(id), opted_out boolean NOT NULL DEFAULT false,
 missed_face boolean NOT NULL DEFAULT false,
 revision bigint NOT NULL DEFAULT 0, actor text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE face_work (
 run_id bigint NOT NULL REFERENCES face_runs(id) ON DELETE CASCADE,
 media_id bigint NOT NULL REFERENCES media(id), content_hash text NOT NULL,
 state text NOT NULL DEFAULT 'pending', generation bigint NOT NULL DEFAULT 0,
 attempts integer NOT NULL DEFAULT 0, error text, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,media_id)
);
CREATE TABLE face_analyses (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 media_id bigint NOT NULL REFERENCES media(id), content_hash text NOT NULL,
 model_id text NOT NULL REFERENCES face_models(id), width integer NOT NULL, height integer NOT NULL,
 outcome text NOT NULL, timings jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(media_id,content_hash,model_id)
);
CREATE TABLE face_detections (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 analysis_id bigint NOT NULL REFERENCES face_analyses(id) ON DELETE CASCADE,
 box jsonb NOT NULL, landmarks jsonb NOT NULL DEFAULT '[]', landmark_schema text,
 score double precision NOT NULL, review_state text NOT NULL DEFAULT 'unreviewed'
);
CREATE TABLE face_review_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 media_id bigint NOT NULL REFERENCES media(id), actor text NOT NULL,
 action text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX face_work_state_idx ON face_work(run_id,state,media_id);
CREATE INDEX face_detections_analysis_idx ON face_detections(analysis_id);
CREATE OR REPLACE FUNCTION cleanup_deleted_faces() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.deleted_at IS NOT NULL THEN
  DELETE FROM face_analyses WHERE media_id=NEW.media_id;
  UPDATE face_work SET state='skipped-trash',generation=generation+1 WHERE media_id=NEW.media_id;
  DELETE FROM face_review_events WHERE media_id=NEW.media_id;
  DELETE FROM face_preferences WHERE media_id=NEW.media_id;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER face_permanent_delete AFTER INSERT OR UPDATE OF deleted_at ON media_trash
 FOR EACH ROW EXECUTE FUNCTION cleanup_deleted_faces();
INSERT INTO schema_versions(version) VALUES (14);
