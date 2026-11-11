-- GEONAF - Supabase SQL Editor - 07 export for the parity check (read only: one SELECT).
-- Run after the import, download the result as CSV (button "Download CSV"), then compare locally:
--   python scripts/verify_supabase_export.py --csv <file.csv> --data-dir <data directory>
-- The local check rebuilds every record from record_document, recomputes its fingerprint, compares it
-- with the file registry and compares the SHA-256 of the WKB returned by PostGIS with the local
-- MultiPolygon conversion. The export holds public geoBoundaries data only (no secret).
-- Writes nothing: the first statement makes the whole request a read-only transaction. srid must be 4326;
-- total_rows (same on every row) lets the local check detect a truncated or partial download.
SELECT set_config('transaction_read_only', 'on', true);
SELECT set_config('search_path', 'pg_catalog, extensions, pg_temp', true);

SELECT v.territory_id, v.version, v.kind, v.label, v.legal_status, v.geometry_sha256, v.file_sha256,
       v.storage_key, v.supersedes, v.record_fingerprint, v.record_document::text AS record_document,
       v.retrieved_at_utc, r.source_id, r.product, r.version AS source_version, r.licence_id, r.attribution,
       r.url, r.share_alike, r.reference_year, g.source_geometry_type,
       encode(sha256(ST_AsBinary(g.geom, 'NDR')), 'hex') AS wkb_sha256,
       ST_SRID(g.geom) AS srid, count(*) OVER () AS total_rows
FROM geonaf.territory_versions v
JOIN geonaf.source_releases r ON r.release_id = v.release_id
JOIN geonaf.geometries g ON g.geometry_sha256 = v.geometry_sha256
ORDER BY v.territory_id, v.version;
