-- GEONAF - Supabase SQL Editor - 90 constraint and immutability tests. PROJECT geonaf-test ONLY.
--
-- Refuses to run unless the geonaf schema is marked geonaf-environment:test (03_mark_environment_test.sql).
-- Inserts synthetic rows, tries every forbidden operation, then ALWAYS ends with an error that cancels
-- the whole request: nothing is kept, even on success. Read the result in the error message:
--   "GEONAF TESTS REUSSIS, TOUT EST ANNULE : ..."  or  "GEONAF TESTS EN ECHEC, TOUT EST ANNULE : ...".
-- Each case expects a precise SQLSTATE: 23514 check, 23503 foreign key, 23505 unique, P0001 GEONAF
-- trigger, 22 data exception (wrong geometry type); 'accepte' for the control rows.

SELECT set_config('search_path', 'pg_catalog, extensions, pg_temp', true);

DO $$
BEGIN
  IF coalesce(obj_description(to_regnamespace('geonaf'), 'pg_namespace'), '') <> 'geonaf-environment:test' THEN
    RAISE EXCEPTION 'GEONAF: script reserve au projet de test (schema geonaf non marque geonaf-environment:test)';
  END IF;
END $$;

CREATE TEMP TABLE geonaf_test_results (label text, expected text, obtained text, ok boolean) ON COMMIT DROP;

-- Runs one statement in a subtransaction and records the SQLSTATE it raised (or 'accepte').
CREATE FUNCTION pg_temp.expect(label text, expected text, statement text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE obtained text;
BEGIN
  BEGIN
    EXECUTE statement;
    obtained := 'accepte';
  EXCEPTION WHEN others THEN
    obtained := SQLSTATE;
  END;
  INSERT INTO geonaf_test_results VALUES (label, expected, obtained, obtained LIKE expected || '%');
END $$;

-- Synthetic reference rows (cancelled with the rest).
INSERT INTO geonaf.data_sources (source_id) VALUES ('geonaf-test-source');
INSERT INTO geonaf.source_releases (source_id, product, version, licence_id, attribution, url, share_alike, reference_year)
VALUES ('geonaf-test-source', 'Produit de test', '1', 'LicenseRef-GEONAF', 'GEONAF', NULL, false, NULL);
INSERT INTO geonaf.geometries (geometry_sha256, identity_version, source_geometry_type, geom, parts, vertices)
VALUES (repeat('9', 64), 'geonaf-aoi-geometry-v1', 'Polygon',
        ST_GeomFromText('MULTIPOLYGON(((9.4 0.4,9.45 0.4,9.45 0.45,9.4 0.45,9.4 0.4)))', 4326), 1, 5);
INSERT INTO geonaf.territories (territory_id, kind) VALUES ('geonaf-test-zone', 'acquisition_zone'),
                                                          ('geonaf-test-unit', 'admin_unit');
INSERT INTO geonaf.registry_imports (source_label, code_version) VALUES ('tests de contraintes', 'test');

-- A valid version row as JSON; each case overrides some fields. The document keys checked by the table
-- (territory_id, version, geometry_sha256, file_sha256) follow the row unless the case sets the document.
CREATE TEMP TABLE geonaf_test_base ON COMMIT DROP AS
SELECT jsonb_build_object(
  'territory_id', 'geonaf-test-zone', 'version', 1, 'kind', 'acquisition_zone',
  'label', 'Zone de test GEONAF', 'legal_status', 'zone d''acquisition GEONAF',
  'release_id', (SELECT release_id FROM geonaf.source_releases WHERE source_id = 'geonaf-test-source'),
  'geometry_sha256', repeat('9', 64), 'file_sha256', repeat('8', 64),
  'storage_key', 'geometries/' || repeat('9', 64) || '.geojson',
  'wgs84_bounds', jsonb_build_array(9.4, 0.4, 9.45, 0.45), 'area_km2', 30.8, 'vertices', 5, 'parts', 1,
  'created_at_utc', '2026-11-02T00:00:00+00:00', 'limits', jsonb_build_array(), 'schema', 'geonaf.territory/v1',
  'record_fingerprint', repeat('1', 64), 'record_document', '{}'::jsonb,
  'import_id', currval(pg_get_serial_sequence('geonaf.registry_imports', 'import_id'))) AS base_row;

CREATE FUNCTION pg_temp.version_insert(overrides jsonb) RETURNS text LANGUAGE plpgsql AS $$
DECLARE candidate jsonb := (SELECT base_row FROM geonaf_test_base) || overrides;
BEGIN
  IF NOT overrides ? 'record_document' THEN
    candidate := candidate || jsonb_build_object('record_document', jsonb_build_object(
      'territory_id', candidate -> 'territory_id', 'version', candidate -> 'version',
      'geometry_sha256', candidate -> 'geometry_sha256', 'file_sha256', candidate -> 'file_sha256'));
  END IF;
  RETURN 'INSERT INTO geonaf.territory_versions SELECT * FROM jsonb_populate_record(NULL::geonaf.territory_versions, '
         || quote_literal(candidate::text) || '::jsonb)';
END $$;

SELECT pg_temp.expect('temoin : version 1 valide', 'accepte', pg_temp.version_insert('{}'));
SELECT pg_temp.expect('temoin : version 2 valide', 'accepte', pg_temp.version_insert(jsonb_build_object(
  'version', 2, 'supersedes', repeat('1', 64), 'record_fingerprint', repeat('2', 64))));
SELECT pg_temp.expect('officiel sans justificatif', '23514', pg_temp.version_insert(jsonb_build_object(
  'territory_id', 'geonaf-test-unit', 'kind', 'admin_unit', 'label', 'Unite de test',
  'legal_status', 'officiel vérifié', 'country_iso3', 'GAB', 'admin_level_code', 'ADM2', 'local_type', 'Department',
  'record_fingerprint', repeat('3', 64))));
SELECT pg_temp.expect('mot administratif dans une zone d''acquisition', '23514', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64),
                     'label', 'Commune de test')));
SELECT pg_temp.expect('information administrative sur une zone d''acquisition', '23514', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64),
                     'country_iso3', 'GAB')));
SELECT pg_temp.expect('statut juridique d''un autre type', '23503', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64),
                     'legal_status', 'référentiel communautaire')));
SELECT pg_temp.expect('saut de version', 'P0001', pg_temp.version_insert(
  jsonb_build_object('version', 5, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64))));
SELECT pg_temp.expect('predecesseur incorrect', 'P0001', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('e', 64), 'record_fingerprint', repeat('4', 64))));
SELECT pg_temp.expect('parties incoherentes avec la geometrie', 'P0001', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64),
                     'parts', 2)));
SELECT pg_temp.expect('changement de type d''un territoire', '23503', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64),
                     'kind', 'admin_unit', 'legal_status', 'référentiel communautaire', 'country_iso3', 'GAB',
                     'admin_level_code', 'ADM2', 'local_type', 'Department')));
SELECT pg_temp.expect('cle de stockage absolue', '23514', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64),
                     'storage_key', 'C:/data/x.geojson')));
SELECT pg_temp.expect('document d''origine incoherent', '23514', pg_temp.version_insert(
  jsonb_build_object('version', 3, 'supersedes', repeat('2', 64), 'record_fingerprint', repeat('4', 64),
                     'record_document', jsonb_build_object('territory_id', 'geonaf-test-zone', 'version', 9,
                       'geometry_sha256', repeat('9', 64), 'file_sha256', repeat('8', 64)))));
SELECT pg_temp.expect('version deja presente avec un autre contenu', '23505', pg_temp.version_insert(
  jsonb_build_object('record_fingerprint', repeat('5', 64))));
SELECT pg_temp.expect('geometrie invalide (anneau auto-intersectant)', '23514',
  $sql$INSERT INTO geonaf.geometries VALUES (repeat('a', 64), 'geonaf-aoi-geometry-v1', 'Polygon',
       ST_GeomFromText('MULTIPOLYGON(((0 0,1 1,1 0,0 1,0 0)))', 4326), 1, 5)$sql$);
SELECT pg_temp.expect('Polygon dans une colonne MultiPolygon', '22',
  $sql$INSERT INTO geonaf.geometries VALUES (repeat('b', 64), 'geonaf-aoi-geometry-v1', 'Polygon',
       ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 4326), 1, 5)$sql$);
SELECT pg_temp.expect('modification d''une version', 'P0001',
  $sql$UPDATE geonaf.territory_versions SET label = 'Autre' WHERE territory_id = 'geonaf-test-zone'$sql$);
SELECT pg_temp.expect('suppression d''une geometrie', 'P0001',
  $sql$DELETE FROM geonaf.geometries WHERE geometry_sha256 = repeat('9', 64)$sql$);
SELECT pg_temp.expect('vidage des versions', 'P0001', $sql$TRUNCATE geonaf.territory_versions$sql$);
SELECT pg_temp.expect('modification d''une licence', 'P0001',
  $sql$UPDATE geonaf.licences SET name = 'Autre' WHERE licence_id = 'LicenseRef-GEONAF'$sql$);
SELECT pg_temp.expect('cloture du journal d''import', 'accepte',
  $sql$UPDATE geonaf.registry_imports SET finished_at = now()
       WHERE import_id = currval(pg_get_serial_sequence('geonaf.registry_imports', 'import_id'))$sql$);
SELECT pg_temp.expect('reouverture d''un journal cloture', 'P0001',
  $sql$UPDATE geonaf.registry_imports SET records_seen = 99
       WHERE import_id = currval(pg_get_serial_sequence('geonaf.registry_imports', 'import_id'))$sql$);

-- Always cancels everything; the message gives the outcome.
DO $$
DECLARE failures text := (SELECT string_agg(label || ' (attendu ' || expected || ', obtenu ' || obtained || ')', ' ; ')
                          FROM geonaf_test_results WHERE NOT ok);
        total integer := (SELECT count(*) FROM geonaf_test_results);
BEGIN
  IF failures IS NULL THEN
    RAISE EXCEPTION 'GEONAF TESTS REUSSIS, TOUT EST ANNULE : % cas conformes', total;
  END IF;
  RAISE EXCEPTION 'GEONAF TESTS EN ECHEC, TOUT EST ANNULE : %', failures;
END $$;
