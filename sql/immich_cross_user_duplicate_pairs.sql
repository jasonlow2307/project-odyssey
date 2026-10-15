-- Read-only psql script for Immich cross-user duplicate pairs.
--
-- It is intended to run inside the immich_postgres container, where the
-- existing local PostgreSQL socket/authentication already works. It writes
-- two CSV files inside that container and makes no database changes.
--
-- Optional psql variables:
--   source_limit     number of source images for the visual trial (default 200)
--   candidate_limit  nearest candidates per source (default 64)
--   max_distance     cosine distance threshold (default 0.01)
--   statement_timeout_ms (default 600000)
--   skip_visual     true to produce only the exact checksum CSV (default false)

\set ON_ERROR_STOP on

\if :{?source_limit}
\else
\set source_limit 200
\endif
\if :{?candidate_limit}
\else
\set candidate_limit 64
\endif
\if :{?max_distance}
\else
\set max_distance 0.01
\endif
\if :{?statement_timeout_ms}
\else
\set statement_timeout_ms 600000
\endif
\if :{?skip_visual}
\else
\set skip_visual false
\endif

BEGIN;
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SET LOCAL search_path TO public;
SELECT set_config('statement_timeout', (:'statement_timeout_ms' || 'ms'), true) AS _statement_timeout \gset

-- Exact checksum matches. This includes assets without smart_search rows.
\pset format csv
\pset footer off
\o /tmp/immich_exact_duplicates.csv
    WITH eligible AS (
        SELECT
            a."id"::text AS asset_id,
            a."ownerId"::text AS owner_id,
            COALESCE(u."name", a."ownerId"::text) AS owner_name,
            a."originalFileName" AS filename,
            a."originalPath" AS original_path,
            e."fileSizeInByte" AS file_size_bytes,
            a."localDateTime" AS local_date_time,
            encode(a."checksum", 'hex') AS checksum_sha1,
            a."type" AS asset_type
        FROM "asset" AS a
        LEFT JOIN "user" AS u ON u."id" = a."ownerId"
        LEFT JOIN "asset_exif" AS e ON e."assetId" = a."id"
        WHERE a."deletedAt" IS NULL
          AND a."stackId" IS NULL
          AND a."visibility" IN ('timeline', 'archive')
          AND a."type" = 'IMAGE'
          AND a."checksum" IS NOT NULL
    ), cross_user_checksums AS (
        SELECT checksum_sha1
        FROM eligible
        GROUP BY checksum_sha1
        HAVING COUNT(DISTINCT owner_id) > 1
    )
    SELECT
        'exact' AS match_kind,
        e.checksum_sha1,
        NULL::double precision AS cosine_distance,
        e.asset_id,
        e.owner_id,
        e.owner_name,
        e.filename,
        e.original_path,
        e.file_size_bytes,
        e.local_date_time,
        e.asset_type
    FROM eligible AS e
    INNER JOIN cross_user_checksums AS c
        ON c.checksum_sha1 = e.checksum_sha1
    ORDER BY e.checksum_sha1, e.owner_id, e.asset_id;
\o

\if :skip_visual
\echo 'Skipping visual search; exact CSV was created.'
\else
-- Visual matches. This is deliberately a bounded trial: it searches only
-- source_limit images, but searches each source against all eligible owners.
-- The <=> operator is pgvector cosine distance and uses Immich's existing
-- smart_search embedding index when available.
\o /tmp/immich_visual_duplicates.csv
    WITH sources AS MATERIALIZED (
        SELECT
            a."id"::text AS asset_id,
            a."ownerId"::text AS owner_id,
            COALESCE(u."name", a."ownerId"::text) AS owner_name,
            a."originalFileName" AS filename,
            a."originalPath" AS original_path,
            e."fileSizeInByte" AS file_size_bytes,
            a."localDateTime" AS local_date_time,
            encode(a."checksum", 'hex') AS checksum_sha1,
            a."type" AS asset_type,
            ss."embedding" AS embedding
        FROM "asset" AS a
        INNER JOIN "smart_search" AS ss ON ss."assetId" = a."id"
        LEFT JOIN "user" AS u ON u."id" = a."ownerId"
        LEFT JOIN "asset_exif" AS e ON e."assetId" = a."id"
        WHERE a."deletedAt" IS NULL
          AND a."stackId" IS NULL
          AND a."visibility" IN ('timeline', 'archive')
          AND a."type" = 'IMAGE'
        ORDER BY a."id"
        LIMIT :source_limit
    )
    SELECT
        'visual' AS match_kind,
        source.checksum_sha1 AS source_checksum_sha1,
        candidate.distance AS cosine_distance,
        source.asset_id AS source_asset_id,
        source.owner_id AS source_owner_id,
        source.owner_name AS source_owner_name,
        source.filename AS source_filename,
        source.original_path AS source_original_path,
        source.file_size_bytes AS source_file_size_bytes,
        source.local_date_time AS source_local_date_time,
        candidate.asset_id AS candidate_asset_id,
        candidate.owner_id AS candidate_owner_id,
        candidate.owner_name AS candidate_owner_name,
        candidate.filename AS candidate_filename,
        candidate.original_path AS candidate_original_path,
        candidate.file_size_bytes AS candidate_file_size_bytes,
        candidate.local_date_time AS candidate_local_date_time,
        candidate.checksum_sha1 AS candidate_checksum_sha1
    FROM sources AS source
    CROSS JOIN LATERAL (
        -- Keep the vector search itself on smart_search so PostgreSQL can use
        -- Immich's pgvector index before joining/filtering asset metadata.
        SELECT
            c."id"::text AS asset_id,
            c."ownerId"::text AS owner_id,
            COALESCE(cu."name", c."ownerId"::text) AS owner_name,
            c."originalFileName" AS filename,
            c."originalPath" AS original_path,
            ce."fileSizeInByte" AS file_size_bytes,
            c."localDateTime" AS local_date_time,
            encode(c."checksum", 'hex') AS checksum_sha1,
            nearest.distance
        FROM (
            SELECT
                cs."assetId" AS asset_id,
                cs."embedding" <=> source.embedding AS distance
            FROM "smart_search" AS cs
            WHERE cs."embedding" IS NOT NULL
            ORDER BY cs."embedding" <=> source.embedding
            LIMIT (:candidate_limit * 8)
        ) AS nearest
        INNER JOIN "asset" AS c ON c."id" = nearest.asset_id
        LEFT JOIN "user" AS cu ON cu."id" = c."ownerId"
        LEFT JOIN "asset_exif" AS ce ON ce."assetId" = c."id"
        WHERE c."deletedAt" IS NULL
          AND c."stackId" IS NULL
          AND c."visibility" IN ('timeline', 'archive')
          AND c."type" = source.asset_type
          AND c."id"::text <> source.asset_id
          AND c."ownerId"::text <> source.owner_id
          AND encode(c."checksum", 'hex') IS DISTINCT FROM source.checksum_sha1
          AND nearest.distance <= :max_distance
        ORDER BY nearest.distance
        LIMIT :candidate_limit
    ) AS candidate;
\o
\endif

ROLLBACK;
\echo 'Created /tmp/immich_exact_duplicates.csv and /tmp/immich_visual_duplicates.csv inside immich_postgres.'
