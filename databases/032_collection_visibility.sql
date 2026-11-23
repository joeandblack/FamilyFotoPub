DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=32) THEN
ALTER TABLE album_collections ADD COLUMN show_on_albums boolean NOT NULL DEFAULT true;
ALTER TABLE album_collections ADD COLUMN albums_sort_order bigint;
UPDATE album_collections SET albums_sort_order=sort_order WHERE parent_id IS NULL;
ALTER TABLE album_collections ADD CONSTRAINT collection_visible_without_parent
 CHECK (parent_id IS NOT NULL OR show_on_albums);
INSERT INTO schema_versions(version) VALUES (32);
END IF;
END $$;
