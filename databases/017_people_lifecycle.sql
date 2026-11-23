ALTER TABLE people ADD COLUMN deleted_at timestamptz;
CREATE TABLE people_lifecycle_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,person_id bigint NOT NULL REFERENCES people(id),action text NOT NULL CHECK(action IN ('delete','restore')),actor text NOT NULL,old_name text NOT NULL,new_name text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_versions(version) VALUES(17);
