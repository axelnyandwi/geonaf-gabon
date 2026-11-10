-- GEONAF - Supabase SQL Editor - 13 rollback: remove the Edge reader role (D5). REQUIRES EXPLICIT VALIDATION.
-- SIMULATION BY DEFAULT: nothing is kept until "application" is true.
--
-- Removes ONLY what 13_edge_reader_role.sql creates: the two policies geonaf_edge_reader_select, the role's
-- privileges in schema geonaf, then the role. No table, no row, no other role is touched. The Edge Function
-- then answers 503 database_unavailable. Delete the secret GEONAF_EDGE_DATABASE_URL afterwards.
-- DROP ROLE fails (and everything is rolled back) if the role still owns objects or holds other privileges:
-- this is wanted, it reveals a grant that 13 did not make.
-- Safety (D2.9): one DO statement plus a read-only SELECT; writes exist only as format() templates.
DO $edge$
DECLARE
  application constant boolean := false; -- true ONLY after a successful simulation and explicit validation.
  edge_role constant text := 'geonaf_edge_reader';
  edge_table text;
BEGIN
  PERFORM pg_catalog.set_config('search_path', '', true);
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = edge_role) THEN
    RAISE NOTICE 'GEONAF: role Edge absent, rien a retirer';
    RETURN;
  END IF;
  FOREACH edge_table IN ARRAY ARRAY['acquisitions', 'acquisition_manifests'] LOOP
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname = 'geonaf' AND tablename = edge_table
               AND policyname = 'geonaf_edge_reader_select') THEN
      EXECUTE format('DROP POLICY geonaf_edge_reader_select ON geonaf.%I', edge_table);
    END IF;
  END LOOP;
  IF pg_catalog.to_regnamespace('geonaf') IS NOT NULL THEN
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA geonaf FROM %I', edge_role);
    EXECUTE format('REVOKE ALL ON SCHEMA geonaf FROM %I', edge_role);
  END IF;
  EXECUTE format('DROP ROLE %I', edge_role);
  IF NOT application THEN
    RAISE EXCEPTION 'GEONAF: SIMULATION REUSSIE, aucune modification conservee (passer application a true)';
  END IF;
END $edge$;

SELECT count(*) AS roles_edge_restants FROM pg_catalog.pg_roles WHERE rolname = 'geonaf_edge_reader';
