CREATE TABLE IF NOT EXISTS auto_people_face_quality(
 detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
 policy text NOT NULL,
 metrics jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('blurry','released')),
 created_at timestamptz NOT NULL DEFAULT now(),
 released_at timestamptz);
