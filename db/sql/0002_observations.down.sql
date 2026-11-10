-- GEONAF migration 0002, downgrade (local databases only: Alembic). Removes only what 0002 created.
DROP VIEW IF EXISTS geonaf.acquisitions_latest;
DROP TABLE IF EXISTS geonaf.indicator_results, geonaf.indicator_definitions, geonaf.indicator_result_kinds,
                     geonaf.acquisition_manifests, geonaf.acquisitions, geonaf.data_imports;
DROP FUNCTION IF EXISTS geonaf.close_data_import_only(), geonaf.check_acquisition_manifest();
