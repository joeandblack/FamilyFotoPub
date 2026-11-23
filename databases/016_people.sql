CREATE TABLE people_models(id text PRIMARY KEY,manifest jsonb NOT NULL);
CREATE TABLE people_runs(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,model_id text NOT NULL REFERENCES people_models(id),actor text NOT NULL,state text NOT NULL DEFAULT 'queued',error text,heartbeat_at timestamptz,active_seconds double precision NOT NULL DEFAULT 0,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE people_work(run_id bigint REFERENCES people_runs(id) ON DELETE CASCADE,detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,media_id bigint REFERENCES media(id),content_hash text NOT NULL,state text NOT NULL DEFAULT 'pending',generation bigint NOT NULL DEFAULT 0,attempts int NOT NULL DEFAULT 0,error text,updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(run_id,detection_id));
CREATE TABLE people_embeddings(detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,model_id text REFERENCES people_models(id),vector jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(detection_id,model_id));
CREATE TABLE people(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,name text NOT NULL CHECK(length(name) BETWEEN 1 AND 100),created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE people_assignments(detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,person_id bigint NOT NULL REFERENCES people(id));
CREATE TABLE people_constraints(a bigint REFERENCES face_detections(id) ON DELETE CASCADE,b bigint REFERENCES face_detections(id) ON DELETE CASCADE,PRIMARY KEY(a,b),CHECK(a<b));
CREATE TABLE people_suggestions(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,run_id bigint REFERENCES people_runs(id) ON DELETE CASCADE,model_id text REFERENCES people_models(id),detection_ids jsonb NOT NULL,min_similarity double precision NOT NULL);
CREATE TABLE people_revision(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),revision bigint NOT NULL DEFAULT 0);
INSERT INTO people_revision DEFAULT VALUES;
CREATE TABLE people_previews(token text PRIMARY KEY,revision bigint NOT NULL,payload jsonb NOT NULL,actor text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE people_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,action text NOT NULL,actor text NOT NULL,before_state jsonb NOT NULL,after_revision bigint NOT NULL,undone boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_versions(version) VALUES(16);
-- JSON snapshots contain private derived references; remove them on face deletion.
CREATE FUNCTION cleanup_deleted_people_face() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 DELETE FROM people_previews;
 DELETE FROM people_events WHERE EXISTS(SELECT 1 FROM jsonb_array_elements(before_state->'assignments') x WHERE (x->>'detection_id')::bigint=OLD.id) OR EXISTS(SELECT 1 FROM jsonb_array_elements(before_state->'constraints') x WHERE (x->>'a')::bigint=OLD.id OR (x->>'b')::bigint=OLD.id);
 DELETE FROM people_suggestions WHERE detection_ids @> jsonb_build_array(OLD.id);
 UPDATE people_revision SET revision=revision+1;
 RETURN OLD;
END $$;
CREATE TRIGGER people_face_cleanup BEFORE DELETE ON face_detections FOR EACH ROW EXECUTE FUNCTION cleanup_deleted_people_face();
