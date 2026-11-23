-- DRAFT ONLY: not registered in app.database.migrate. Disposable-schema tests only.
CREATE TABLE auto_people_face_quality(
 detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
 policy text NOT NULL,metrics jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('blurry','released')),
 created_at timestamptz NOT NULL DEFAULT now(),released_at timestamptz);
CREATE TABLE auto_people_scan_results(
 analysis_id bigint PRIMARY KEY REFERENCES face_analyses(id) ON DELETE CASCADE,
 items jsonb NOT NULL);
CREATE TABLE auto_people_groups(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 person_id bigint UNIQUE REFERENCES people(id),created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE auto_people_members(
 detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
 group_id bigint NOT NULL REFERENCES auto_people_groups(id),
 origin text NOT NULL CHECK(origin IN ('automatic','named_seed','manual')),
 model_id text NOT NULL REFERENCES people_models(id));
CREATE TABLE auto_people_observations(
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 model_id text REFERENCES people_models(id),seed boolean NOT NULL,
 PRIMARY KEY(detection_id,model_id));
CREATE TABLE auto_people_blocks(
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 person_id bigint REFERENCES people(id),PRIMARY KEY(detection_id,person_id));
CREATE TABLE auto_people_receipts(
 id text PRIMARY KEY,input_hash text NOT NULL,model_id text NOT NULL REFERENCES people_models(id),
 result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE auto_people_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,action text NOT NULL,
 origin text NOT NULL CHECK(origin IN ('automatic','manual')),actor text NOT NULL,
 before_state jsonb NOT NULL,after_revision bigint NOT NULL,undone boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now());
