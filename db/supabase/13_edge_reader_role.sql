-- GEONAF - Supabase SQL Editor - 13 dedicated reader role of the Edge Function geonaf-acquisitions (D5).
-- PROPOSAL, REQUIRES EXPLICIT VALIDATION. SIMULATION BY DEFAULT: nothing is kept until "application" is true.
--
-- Why a new role: geonaf_reader stays NOLOGIN (10_security_precheck line 21). The Edge Function connects
-- through the transaction pooler (port 6543) as geonaf_edge_reader.<project-ref>, which needs a LOGIN role.
-- Membership in geonaf_reader is NOT used: an RLS policy "TO geonaf_reader" applies to another role only
-- if it has the privileges of geonaf_reader (INHERIT, and from PostgreSQL 16 the INHERIT option of each
-- grant). This role therefore receives its own column grants and its own policies, and is refused if it is
-- a member of any role.
--
-- Least privilege: USAGE on schema geonaf, SELECT on the exact columns the function returns or joins on,
-- in geonaf.acquisitions and geonaf.acquisition_manifests only. Never manifest_document, stac_source,
-- footprint, import_id, the import journal, indicators or territories. No PostGIS, no function.
-- NOINHERIT, NOBYPASSRLS, CONNECTION LIMIT 5, read-only transactions and timeouts as role defaults
-- (the function also sets them per transaction: role defaults are only a second barrier).
--
-- The role is created NOLOGIN, without password. LOGIN and the credential are a separate step
-- (scripts/edge_role_credential.py, docs/edge_acquisitions_d5.md): no password is ever written here.
-- Safety (D2.9): one DO statement plus a read-only SELECT; every write exists only as a format() template,
-- so a partial selection is a syntax error or the read-only SELECT. Idempotent. Undo: 13_edge_reader_role_rollback.sql.
DO $edge$
DECLARE
  application constant boolean := false; -- true ONLY after a successful simulation and explicit validation.
  edge_role constant text := 'geonaf_edge_reader';
  acquisition_columns constant text := 'collection, product_id, acquired_at';
  manifest_columns constant text :=
    'manifest_sha256, collection, product_id, ingested_at, grid_crs, grid_resolution_m, integrity_mode';
  edge_table text;
BEGIN
  PERFORM pg_catalog.set_config('search_path', '', true);
  IF pg_catalog.obj_description(pg_catalog.to_regnamespace('geonaf'), 'pg_namespace')
     IS DISTINCT FROM 'geonaf-environment:test' THEN
    RAISE EXCEPTION 'GEONAF: projet non marque geonaf-environment:test, rien n''est fait';
  END IF;
  IF pg_catalog.to_regclass('geonaf.acquisitions') IS NULL
     OR pg_catalog.to_regclass('geonaf.acquisition_manifests') IS NULL THEN
    RAISE EXCEPTION 'GEONAF: migration 0002 absente (etat S5 attendu), rien n''est fait';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = edge_role) THEN
    EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION '
                   'NOBYPASSRLS CONNECTION LIMIT 5', edge_role);
  ELSIF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = edge_role
                AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication OR rolinherit)) THEN
    RAISE EXCEPTION 'GEONAF: role Edge existant avec des attributs non conformes, rien n''est fait';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid = m.member
             WHERE r.rolname = edge_role) THEN
    RAISE EXCEPTION 'GEONAF: le role Edge est membre d''un autre role (droits herites), rien n''est fait';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname = 'geonaf' AND 'public' = ANY (roles)) THEN
    RAISE EXCEPTION 'GEONAF: politique RLS TO PUBLIC dans geonaf, elle s''appliquerait aussi au role Edge';
  END IF;

  EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', edge_role, '5s');
  EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', edge_role, '10s');
  EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = %L', edge_role, 'on');
  EXECUTE format('GRANT USAGE ON SCHEMA geonaf TO %I', edge_role);
  EXECUTE format('GRANT SELECT (%s) ON geonaf.acquisitions TO %I', acquisition_columns, edge_role);
  EXECUTE format('GRANT SELECT (%s) ON geonaf.acquisition_manifests TO %I', manifest_columns, edge_role);
  FOREACH edge_table IN ARRAY ARRAY['acquisitions', 'acquisition_manifests'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname = 'geonaf' AND tablename = edge_table
                   AND policyname = 'geonaf_edge_reader_select') THEN
      EXECUTE format('CREATE POLICY geonaf_edge_reader_select ON geonaf.%I FOR SELECT TO %I USING (true)',
                     edge_table, edge_role);
    END IF;
  END LOOP;

  IF NOT application THEN
    RAISE EXCEPTION 'GEONAF: SIMULATION REUSSIE, aucune modification conservee (passer application a true)';
  END IF;
  RAISE NOTICE 'GEONAF: role Edge et droits en place (sans LOGIN), executer 13_edge_reader_role_verify.sql';
END $edge$;

SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolinherit, rolconnlimit FROM pg_catalog.pg_roles
WHERE rolname = 'geonaf_edge_reader';
