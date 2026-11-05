-- GEONAF - Supabase SQL Editor - 01b USAGE on the PostGIS schema for the GEONAF roles. REQUIRES EXPLICIT VALIDATION.
--
-- Run ONLY if 10_security_precheck.sql line 50 (or 11_state_diagnostic.sql, rows "droits ... PostGIS") shows that
-- geonaf_reader or geonaf_importer cannot use the schema where PostGIS is installed: calling ST_* needs USAGE
-- on that schema. Nothing is assumed about the platform's defaults: the grant is made only where the
-- effective privilege (has_schema_privilege, PUBLIC and inherited grants included) is missing.
--
-- Least privilege: USAGE only. Never CREATE, never ALL, never on another schema; EXECUTE on the PostGIS
-- functions is left as installed (10 line 51 checks it). Idempotent: a second run changes nothing.
-- Safety (D2.9): one DO statement plus a read-only SELECT; the GRANT exists only as a format() template,
-- so a partial selection is a syntax error or the read-only SELECT. Undo (decision required):
-- REVOKE USAGE on that schema from the two roles, only if this script granted it (line "accorde" below).
DO $$
DECLARE
  postgis_schema text := (SELECT n.nspname FROM pg_catalog.pg_extension e
                          JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis');
  geonaf_role text;
BEGIN
  IF postgis_schema IS NULL THEN
    RAISE EXCEPTION 'GEONAF: extension PostGIS absente, rien a accorder';
  END IF;
  FOREACH geonaf_role IN ARRAY ARRAY['geonaf_reader', 'geonaf_importer'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = geonaf_role) THEN
      RAISE EXCEPTION 'GEONAF: role GEONAF absent, executer 01_roles.sql d''abord' USING DETAIL = geonaf_role;
    END IF;
    IF NOT has_schema_privilege(geonaf_role, postgis_schema, 'USAGE') THEN
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', postgis_schema, geonaf_role);
      RAISE NOTICE 'GEONAF: USAGE accorde sur % a %', postgis_schema, geonaf_role;
    END IF;
  END LOOP;
END $$;

SELECT r.rolname AS role, n.nspname AS schema_postgis,
       has_schema_privilege(r.rolname, n.oid, 'USAGE') AS usage,
       has_schema_privilege(r.rolname, n.oid, 'CREATE') AS creation_interdite_si_faux
FROM pg_catalog.pg_roles r, pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname = 'postgis' AND r.rolname IN ('geonaf_reader', 'geonaf_importer')
ORDER BY r.rolname;
