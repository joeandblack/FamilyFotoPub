-- Duplicate encounters remain counted audit rows, not library membership.
-- Require a retained original before removing any secondary association.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=24) THEN
    IF EXISTS (SELECT 1 FROM import_items i WHERE i.state='duplicate'
        AND i.media_id IS NOT NULL AND NOT EXISTS
        (SELECT 1 FROM import_items original WHERE original.media_id=i.media_id
         AND original.state='imported')) THEN
      RAISE EXCEPTION 'Duplicate association lacks a retained original; review required';
    END IF;
    UPDATE import_items SET media_id=NULL WHERE state='duplicate' AND media_id IS NOT NULL;
    ALTER TABLE jobs DROP COLUMN import_mode;
    INSERT INTO schema_versions(version) VALUES(24);
  END IF;
END $$;
