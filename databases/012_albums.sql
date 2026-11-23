CREATE TABLE IF NOT EXISTS albums (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
 description text NOT NULL DEFAULT '' CHECK (length(description)<=4000),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS album_items (
 album_id bigint NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
 media_id bigint NOT NULL REFERENCES media(id) ON DELETE CASCADE,
 position bigint NOT NULL,
 is_cover boolean NOT NULL DEFAULT false,
 PRIMARY KEY(album_id,media_id)
);
CREATE INDEX IF NOT EXISTS album_items_order_idx ON album_items(album_id,position,media_id);
CREATE INDEX IF NOT EXISTS album_items_media_idx ON album_items(media_id);
CREATE UNIQUE INDEX IF NOT EXISTS album_one_cover_idx ON album_items(album_id) WHERE is_cover;
CREATE OR REPLACE FUNCTION cleanup_deleted_album_membership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.deleted_at IS NOT NULL THEN
  DELETE FROM album_items WHERE media_id=NEW.media_id;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS album_permanent_delete ON media_trash;
CREATE TRIGGER album_permanent_delete AFTER INSERT OR UPDATE OF deleted_at ON media_trash
 FOR EACH ROW EXECUTE FUNCTION cleanup_deleted_album_membership();
INSERT INTO schema_versions(version) VALUES (12) ON CONFLICT DO NOTHING;
