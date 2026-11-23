DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=27) THEN
CREATE TABLE album_collections (
    id bigserial PRIMARY KEY,
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    description text NOT NULL DEFAULT '' CHECK (length(description)<=4000),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE album_collection_members (
    collection_id bigint NOT NULL REFERENCES album_collections(id) ON DELETE CASCADE,
    album_id bigint NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    position bigint NOT NULL,
    PRIMARY KEY (collection_id,album_id)
);
CREATE INDEX album_collection_album_idx ON album_collection_members(album_id);
INSERT INTO schema_versions(version) VALUES (27);
END IF;
END $$;
