ALTER TABLE face_runs ADD COLUMN redetect boolean NOT NULL DEFAULT false;
ALTER TABLE face_analyses DROP CONSTRAINT face_analyses_media_id_content_hash_model_id_key;
ALTER TABLE face_analyses ADD COLUMN revision bigint NOT NULL DEFAULT 0;
ALTER TABLE face_analyses ADD COLUMN staged boolean NOT NULL DEFAULT false;
ALTER TABLE face_analyses ADD COLUMN run_id bigint REFERENCES face_runs(id);
ALTER TABLE face_analyses ADD UNIQUE(media_id,content_hash,model_id,revision);
ALTER TABLE face_detections ADD COLUMN ignored boolean NOT NULL DEFAULT false;
ALTER TABLE face_detections ADD COLUMN ignore_history boolean NOT NULL DEFAULT false;
ALTER TABLE face_detections ADD COLUMN accepted boolean NOT NULL DEFAULT false;
ALTER TABLE face_detections ADD COLUMN superseded_by bigint REFERENCES face_detections(id) ON DELETE SET NULL;
CREATE TABLE face_redetect_targets(
 run_id bigint REFERENCES face_runs(id) ON DELETE CASCADE,
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 PRIMARY KEY(run_id,detection_id));
CREATE TABLE face_ignore_events(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 detection_id bigint REFERENCES face_detections(id) ON DELETE CASCADE,
 actor text NOT NULL,action text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_versions(version) VALUES(18);
