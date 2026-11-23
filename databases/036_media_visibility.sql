ALTER TABLE media ADD COLUMN IF NOT EXISTS owner_name text;
ALTER TABLE media ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'shared';
DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='media_visibility_valid') THEN
ALTER TABLE media ADD CONSTRAINT media_visibility_valid
 CHECK (visibility IN ('shared','private') AND (visibility='shared' OR owner_name IS NOT NULL));
END IF;
END $$;
CREATE INDEX IF NOT EXISTS media_private_owner_idx ON media(owner_name) WHERE visibility='private';
-- The pool sets familyfoto.scope='user' and familyfoto.user only for non-admin
-- requests; workers, administrators and scripts leave it unset and see everything.
CREATE OR REPLACE FUNCTION familyfoto_can_see(owner text, vis text) RETURNS boolean
LANGUAGE sql STABLE AS $$
 SELECT vis = 'shared'
  OR coalesce(current_setting('familyfoto.scope', true), '') <> 'user'
  OR (owner IS NOT NULL AND owner = current_setting('familyfoto.user', true))
$$;
INSERT INTO schema_versions(version) VALUES (36) ON CONFLICT DO NOTHING;
