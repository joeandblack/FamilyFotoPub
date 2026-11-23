ALTER TABLE jobs ADD COLUMN import_mode text NOT NULL DEFAULT 'normal'
    CHECK (import_mode IN ('normal','missing-only'));
ALTER TABLE jobs ADD COLUMN skipped_duplicate_files bigint NOT NULL DEFAULT 0
    CHECK (skipped_duplicate_files >= 0);
ALTER TABLE jobs ADD COLUMN skipped_duplicate_bytes bigint NOT NULL DEFAULT 0
    CHECK (skipped_duplicate_bytes >= 0);
ALTER TABLE import_items ADD COLUMN recovery_attempted boolean NOT NULL DEFAULT false;
INSERT INTO schema_versions(version) VALUES (19);
