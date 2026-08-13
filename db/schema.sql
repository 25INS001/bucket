-- bucket-service schema.
--
-- Ported from the Django migration storage/migrations/0001_initial.py. Column
-- names and constraints match it exactly, so the Drogon service reads and
-- writes the same table the Python one did — the port is a rewrite of the
-- service, not a migration of its data.
--
-- Safe to run multiple times.
--
--     psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f db/schema.sql
--
-- Note the table name: Django derives it from <app>_<model>, so the existing
-- table is `storage_objectmetadata`. See the rename note at the bottom.

CREATE TABLE IF NOT EXISTS object_metadata (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    bucket            VARCHAR(128) NOT NULL,

    -- The random key the bytes actually live under in S3. Unique because a
    -- collision would silently overwrite somebody else's object.
    object_key        TEXT NOT NULL UNIQUE,

    -- What the uploader called it. Never used as the S3 key: keeping the two
    -- apart is what allows versioning a logical name, and stops a filename
    -- being guessable from a key.
    original_filename VARCHAR(255) NOT NULL,

    version           INTEGER NOT NULL DEFAULT 1,

    size              BIGINT,
    content_type      VARCHAR(128),
    checksum          VARCHAR(128),

    -- Set once the client has completed the presigned PUT. Downloads refuse
    -- until then, so a row created by an upload that never happened cannot be
    -- handed out as a working URL.
    is_uploaded       BOOLEAN NOT NULL DEFAULT FALSE,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The constraint the version-allocation race turns on: two concurrent
    -- uploads that compute the same next version collide here rather than
    -- both being written. ObjectService::createNextVersion retries on it.
    CONSTRAINT object_metadata_version_unique UNIQUE (bucket, original_filename, version),

    CONSTRAINT object_metadata_version_positive CHECK (version > 0)
);

-- findLatest() orders by version within a (bucket, filename); the unique
-- constraint above already indexes that prefix, but DESC ordering benefits from
-- an index in the direction it reads.
CREATE INDEX IF NOT EXISTS idx_object_metadata_latest
    ON object_metadata (bucket, original_filename, version DESC);

-- gen_random_uuid() is built in from PostgreSQL 13; this is here only for
-- older servers, and is a no-op where it is not needed.
-- CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Existing deployments
--
-- The Django table is `storage_objectmetadata`. If it holds rows worth keeping,
-- rename it rather than creating an empty one beside it — otherwise the service
-- starts clean and the old data is invisible while still occupying the object
-- keys in S3:
--
--     ALTER TABLE storage_objectmetadata RENAME TO object_metadata;
--
-- The column names are identical, so nothing else changes. Check first:
--
--     SELECT count(*) FROM storage_objectmetadata;
-- ---------------------------------------------------------------------------
