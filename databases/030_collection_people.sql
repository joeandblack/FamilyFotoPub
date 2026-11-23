DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM schema_versions WHERE version=30) THEN
CREATE TABLE album_collection_people (
 collection_id bigint NOT NULL REFERENCES album_collections(id) ON DELETE CASCADE,
 pilot text NOT NULL CHECK (pilot ~ '^[0-9a-f]{32}$'),
 person_id bigint NOT NULL CHECK (person_id > 0),
 position bigint NOT NULL,
 PRIMARY KEY(collection_id,pilot,person_id)
);
INSERT INTO schema_versions(version) VALUES (30);
END IF;
END $$;
