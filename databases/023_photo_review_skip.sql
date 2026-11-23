ALTER TABLE photo_duplicate_items DROP CONSTRAINT photo_duplicate_items_state_check;
ALTER TABLE photo_duplicate_items ADD CONSTRAINT photo_duplicate_items_state_check CHECK(state IN ('pending','done','warning','skipped'));
INSERT INTO schema_versions(version) VALUES(23);
