-- Additive, one-time backfill. Apply only with all media workers idle.
ALTER TABLE media ADD COLUMN IF NOT EXISTS first_import_job_id bigint REFERENCES jobs(id);
ALTER TABLE media ADD COLUMN IF NOT EXISTS first_import_basis text;
CREATE INDEX IF NOT EXISTS media_first_import_idx ON media(first_import_job_id) WHERE kind='video';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS legacy_postprocess_status jsonb;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=8) THEN
    -- A copied row alone could represent recovery of a missing copy. Require
    -- one copied-import candidate AND the import event active at catalog creation.
    -- Never infer an owner from filenames, inventory IDs, or mutable start times.
    WITH copied AS (
      SELECT media_id,min(job_id) job_id
      FROM import_items WHERE state='imported' AND media_id IS NOT NULL
      GROUP BY media_id HAVING count(DISTINCT job_id)=1
    ), proven AS (
      SELECT m.id,c.job_id FROM media m JOIN copied c ON c.media_id=m.id
      JOIN LATERAL (
        SELECT job_id,event FROM job_events WHERE created_at<=m.created_at
        ORDER BY created_at DESC,id DESC LIMIT 1
      ) e ON e.job_id=c.job_id AND e.event='running'
      WHERE m.kind='video' AND m.first_import_job_id IS NULL
    )
    UPDATE media m SET first_import_job_id=p.job_id,first_import_basis='legacy-copy-and-creation-event'
    FROM proven p WHERE m.id=p.id;

    -- Shared-scope timing/count snapshots cannot be attributed to original work.
    -- Preserve them for audit instead of presenting them as original-only runs.
    UPDATE jobs SET legacy_postprocess_status=jsonb_build_object(
      'metadata',metadata_status,'conversion',conversion_status),
      metadata_status=NULL,conversion_status=NULL
    WHERE metadata_status IS NOT NULL OR conversion_status IS NOT NULL;
    INSERT INTO schema_versions(version) VALUES (8);
  END IF;
END $$;
