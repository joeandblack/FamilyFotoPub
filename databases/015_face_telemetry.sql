-- Existing runs have unknown historical active time; never invent elapsed time.
ALTER TABLE face_runs ADD COLUMN active_seconds double precision;
ALTER TABLE face_runs ALTER COLUMN active_seconds SET DEFAULT 0;
ALTER TABLE face_runs ADD COLUMN telemetry_epoch bigint NOT NULL DEFAULT 0;
INSERT INTO schema_versions(version) VALUES (15);
