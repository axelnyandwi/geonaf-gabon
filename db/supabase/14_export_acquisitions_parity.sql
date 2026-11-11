-- GEONAF - Supabase SQL Editor - 14 export of acquisitions and ingestions for the parity check (read only).
-- Run the whole file in a new query, download the result as CSV, then compare locally (no connection):
--   python scripts/verify_acquisition_export.py --csv <file.csv> --data-dir <data directory>
-- One row per ingestion (geonaf.acquisition_manifests), plus one row per acquisition without any manifest
-- (manifest columns empty). The local check rebuilds every manifest from manifest_document, recomputes its
-- SHA-256, compares it with the strict local manifests, the dates (acquisition and ingestion, never merged),
-- the grid footprint (SRID 4326 and SHA-256 of its WKB) and the number of ingestions of each product.
-- The footprint is the extent of the red band's raster grid, not of its valid pixels.
-- Volume: at most 5000 rows; total_rows is counted before the limit, so a cut export is detected locally.
-- Content: public Copernicus product metadata (STAC source, asset keys); no account, key or local path.
-- Writes nothing: the first statement makes the whole request a read-only transaction.
SELECT set_config('transaction_read_only', 'on', true);
SELECT set_config('search_path', 'pg_catalog, extensions, pg_temp', true);

SELECT m.manifest_sha256::text AS manifest_sha256, a.collection, a.product_id, m.schema_version,
       to_char(m.acquired_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS acquired_at_utc,
       to_char(a.acquired_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS product_acquired_at_utc,
       to_char(m.ingested_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS ingested_at_utc,
       m.stac_source, m.grid_crs, m.grid_resolution_m::text AS grid_resolution_m, m.integrity_mode,
       m.import_id, (d.finished_at IS NOT NULL) AS import_closed, m.manifest_document::text AS manifest_document,
       ST_SRID(m.footprint) AS footprint_srid,
       encode(sha256(ST_AsBinary(m.footprint, 'NDR')), 'hex') AS footprint_wkb_sha256,
       count(m.manifest_sha256) OVER (PARTITION BY a.collection, a.product_id) AS ingestions_of_product,
       count(*) OVER () AS total_rows
FROM geonaf.acquisitions a
LEFT JOIN geonaf.acquisition_manifests m ON m.collection = a.collection AND m.product_id = a.product_id
LEFT JOIN geonaf.data_imports d ON d.import_id = m.import_id
ORDER BY a.collection, a.product_id, m.ingested_at, m.manifest_sha256
LIMIT 5000;
