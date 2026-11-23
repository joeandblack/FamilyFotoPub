CREATE TABLE IF NOT EXISTS media_adjustments (
 media_id bigint PRIMARY KEY REFERENCES media(id),
 rotation integer NOT NULL DEFAULT 0 CHECK(rotation IN (0,90,180,270))
);
INSERT INTO schema_versions(version) VALUES (5) ON CONFLICT DO NOTHING;
