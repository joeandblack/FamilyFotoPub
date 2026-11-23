-- Windows FILETIME includes 1601 and dates beyond signed bigint nanoseconds.
-- Keep exact source timestamps for change detection; never alter source files.
ALTER TABLE import_items ALTER COLUMN mtime_ns TYPE numeric(30,0);
INSERT INTO schema_versions(version) VALUES (13) ON CONFLICT DO NOTHING;
