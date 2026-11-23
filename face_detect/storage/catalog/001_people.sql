-- The People catalog: one schema, independent of the model and computer. In an installation it
-- is named `people`; tests make their own. See docs/FACE_CATALOG_MIGRATION.md.
-- Run with the search path set to the catalog schema, then the main schema (for example
-- `people, public`): names here are unqualified, so each table lands in the catalog and
-- `media` is the main photo list. There is no shadow copy of it. The trigger function pins
-- that search path when it's created, so it never acts on whatever schema a session uses.
-- The keys to media(id) have no ON DELETE action on purpose: anything that removes a
-- photo's row must remove its People rows first (as app/library/face_cleanup.py does), or the
-- removal fails instead of leaving faces behind.
CREATE TABLE catalog_versions(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

-- Where boxes come from. legacy_model_id is the old face_models id.
CREATE TABLE detector(
 detector_id text PRIMARY KEY, manifest jsonb NOT NULL, legacy_model_id text UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now());

-- What vectors mean. A retired space is kept for history and never used for matching.
CREATE TABLE space(
 space_id text PRIMARY KEY, manifest jsonb NOT NULL,
 state text NOT NULL CHECK (state IN ('active','building','retired')),
 legacy_model_id text UNIQUE, created_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX space_one_active ON space((true)) WHERE state='active';

-- A check that a backend gives a space's vectors, or the record of the backend that made them.
-- 'origin' is a record, not a check, and never counts as a passed certificate.
CREATE TABLE certificate(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 space_id text NOT NULL REFERENCES space(space_id),
 backend jsonb NOT NULL, backend_id text NOT NULL,
 reference text NOT NULL CHECK (reference IN ('library-sample','golden-set','origin')),
 passed boolean NOT NULL, result jsonb NOT NULL DEFAULT '{}',
 checked_at timestamptz NOT NULL DEFAULT now(),
 CHECK (reference <> 'origin' OR NOT passed));

CREATE TABLE policy(
 policy_id text PRIMARY KEY, space_id text NOT NULL UNIQUE REFERENCES space(space_id),
 policy jsonb NOT NULL, evidence jsonb NOT NULL, activated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE catalog_state(
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 active_space_id text NOT NULL REFERENCES space(space_id),
 catalog_uuid uuid NOT NULL, migrated_from text, legacy_pilot text,
 created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE face_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 detector_id text NOT NULL REFERENCES detector(detector_id), actor text NOT NULL,
 state text NOT NULL DEFAULT 'queued', error text, heartbeat_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 active_seconds double precision DEFAULT 0, telemetry_epoch bigint NOT NULL DEFAULT 0,
 redetect boolean NOT NULL DEFAULT false);

CREATE TABLE face_work(
 run_id bigint NOT NULL REFERENCES face_runs(id) ON DELETE CASCADE,
 media_id bigint NOT NULL REFERENCES media(id), content_hash text NOT NULL,
 state text NOT NULL DEFAULT 'pending', generation bigint NOT NULL DEFAULT 0,
 attempts integer NOT NULL DEFAULT 0, error text, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,media_id));
CREATE INDEX face_work_state_idx ON face_work(run_id,state,media_id);

CREATE TABLE face_analyses(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 media_id bigint NOT NULL REFERENCES media(id), content_hash text NOT NULL,
 detector_id text NOT NULL REFERENCES detector(detector_id),
 width integer NOT NULL, height integer NOT NULL, outcome text NOT NULL,
 timings jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
 revision bigint NOT NULL DEFAULT 0, staged boolean NOT NULL DEFAULT false,
 run_id bigint REFERENCES face_runs(id),
 UNIQUE(media_id,content_hash,detector_id,revision));
CREATE INDEX face_analyses_media_idx ON face_analyses(media_id);

-- face_uuid is the face's lasting ID across libraries. The numeric id is this library's own key.
CREATE TABLE face_detections(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 face_uuid uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 analysis_id bigint NOT NULL REFERENCES face_analyses(id) ON DELETE CASCADE,
 box jsonb NOT NULL, landmarks jsonb NOT NULL DEFAULT '[]', landmark_schema text,
 score double precision NOT NULL, review_state text NOT NULL DEFAULT 'unreviewed',
 ignored boolean NOT NULL DEFAULT false, ignore_history boolean NOT NULL DEFAULT false,
 accepted boolean NOT NULL DEFAULT false,
 superseded_by bigint REFERENCES face_detections(id) ON DELETE SET NULL);
CREATE INDEX face_detections_analysis_idx ON face_detections(analysis_id);

CREATE TABLE face_review_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 media_id bigint NOT NULL REFERENCES media(id), actor text NOT NULL,
 action text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE face_redetect_targets(
 run_id bigint REFERENCES face_runs(id) ON DELETE CASCADE,
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 PRIMARY KEY(run_id,detection_id));

CREATE TABLE face_ignore_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 actor text NOT NULL, action text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE people_runs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 space_id text NOT NULL REFERENCES space(space_id), actor text NOT NULL,
 state text NOT NULL DEFAULT 'queued', error text, heartbeat_at timestamptz,
 active_seconds double precision NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE people_work(
 run_id bigint REFERENCES people_runs(id) ON DELETE CASCADE,
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 media_id bigint REFERENCES media(id), content_hash text NOT NULL,
 state text NOT NULL DEFAULT 'pending', generation bigint NOT NULL DEFAULT 0,
 attempts integer NOT NULL DEFAULT 0, error text, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,detection_id));

-- A face may have vectors in several spaces. backend records the machinery that made it.
CREATE TABLE embedding(
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 space_id text REFERENCES space(space_id), vector jsonb NOT NULL,
 backend jsonb, certificate_id bigint REFERENCES certificate(id),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(detection_id,space_id));

CREATE TABLE people(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
 created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz);

CREATE TABLE people_assignments(
 detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
 person_id bigint NOT NULL REFERENCES people(id));

CREATE TABLE people_constraints(
 a bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 b bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 PRIMARY KEY(a,b), CHECK(a<b));

CREATE TABLE people_suggestions(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 run_id bigint REFERENCES people_runs(id) ON DELETE CASCADE,
 space_id text REFERENCES space(space_id),
 detection_ids jsonb NOT NULL, min_similarity double precision NOT NULL);

CREATE TABLE people_revision(
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), revision bigint NOT NULL DEFAULT 0);

CREATE TABLE people_previews(
 token text PRIMARY KEY, revision bigint NOT NULL, payload jsonb NOT NULL,
 actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE people_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, action text NOT NULL, actor text NOT NULL,
 before_state jsonb NOT NULL, after_revision bigint NOT NULL, undone boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE people_lifecycle_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 person_id bigint NOT NULL REFERENCES people(id),
 action text NOT NULL CHECK(action IN ('delete','restore')), actor text NOT NULL,
 old_name text NOT NULL, new_name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE auto_people_face_quality(
 detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
 policy text NOT NULL, metrics jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('blurry','released')),
 created_at timestamptz NOT NULL DEFAULT now(), released_at timestamptz);

CREATE TABLE auto_people_scan_results(
 analysis_id bigint PRIMARY KEY REFERENCES face_analyses(id) ON DELETE CASCADE,
 items jsonb NOT NULL);

CREATE TABLE auto_people_groups(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 person_id bigint UNIQUE REFERENCES people(id), created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE auto_people_members(
 detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
 group_id bigint NOT NULL REFERENCES auto_people_groups(id),
 origin text NOT NULL CHECK(origin IN ('automatic','named_seed','manual')),
 space_id text NOT NULL REFERENCES space(space_id));

CREATE TABLE auto_people_observations(
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 space_id text REFERENCES space(space_id), seed boolean NOT NULL,
 PRIMARY KEY(detection_id,space_id));

CREATE TABLE auto_people_blocks(
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 person_id bigint REFERENCES people(id), PRIMARY KEY(detection_id,person_id));

CREATE TABLE auto_people_receipts(
 id text PRIMARY KEY, input_hash text NOT NULL,
 space_id text NOT NULL REFERENCES space(space_id),
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE auto_people_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, action text NOT NULL,
 origin text NOT NULL CHECK(origin IN ('automatic','manual')), actor text NOT NULL,
 before_state jsonb NOT NULL, after_revision bigint NOT NULL, undone boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now());

-- October's record of each face it carried over from CompreFace. Discovery reads it to
-- judge which carried-over faces are clear enough to anchor a person.
CREATE TABLE native_migration_faces(
 detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
 space_id text NOT NULL REFERENCES space(space_id),
 state text NOT NULL CHECK(state IN ('embedded','review')), result jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());

-- Photos whose faces were carried over in October; a scan skips them.
CREATE TABLE native_preserved_photos(media_id bigint, content_hash text);
CREATE UNIQUE INDEX native_preserved_photos_identity ON native_preserved_photos(media_id,content_hash);

-- Short-lived edit tokens, suggestions and history that name a deleted face go with it.
CREATE FUNCTION cleanup_deleted_face() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
 DELETE FROM people_previews;
 DELETE FROM people_events WHERE EXISTS(SELECT 1 FROM jsonb_array_elements(before_state->'assignments') x WHERE (x->>'detection_id')::bigint=OLD.id) OR EXISTS(SELECT 1 FROM jsonb_array_elements(before_state->'constraints') x WHERE (x->>'a')::bigint=OLD.id OR (x->>'b')::bigint=OLD.id);
 DELETE FROM people_suggestions WHERE detection_ids @> jsonb_build_array(OLD.id);
 UPDATE people_revision SET revision=revision+1;
 RETURN OLD;
END $$;
CREATE TRIGGER people_face_cleanup BEFORE DELETE ON face_detections
 FOR EACH ROW EXECUTE FUNCTION cleanup_deleted_face();

INSERT INTO people_revision DEFAULT VALUES;
INSERT INTO catalog_versions(version) VALUES (1);
