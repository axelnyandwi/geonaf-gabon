-- GEONAF - Supabase SQL Editor - 09 rename the legacy geonaf schema (SIMULATION). Writes nothing.
--
-- Non destructive: ALTER SCHEMA ... RENAME keeps every table, column, constraint, index, sequence, grant,
-- comment and row (objects are addressed by OID, not by name). Nothing is dropped.
-- Refuses to run unless the schema is exactly the legacy one found on 2026-11-03 (8 known tables, empty,
-- no environment mark) and nothing outside it refers to it (same blocking checks as 08).
-- Order per project: 08, 09 simulation, 09 application, then 00, 01, 02, 03, 04 (docs/supabase_legacy_schema_plan.md).
-- One request, no BEGIN/COMMIT: any error cancels everything. Run once per project.
-- Reverse, only while migration 0001 has not been applied: ALTER SCHEMA geonaf_legacy_draft RENAME TO geonaf;
DO $$
DECLARE
  legacy oid := to_regnamespace('geonaf');
  expected text := 'acquisitions,analysis_acquisitions,analysis_runs,indicator_definitions,indicator_results,sources,territories,territory_versions';
  found text;
  row_count bigint;
  blockers text[] := '{}';
  t record;
BEGIN
  IF legacy IS NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf absent, rien a renommer';
  END IF;
  IF obj_description(legacy, 'pg_namespace') IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf marque, ce n''est pas le schema herite'
      USING DETAIL = obj_description(legacy, 'pg_namespace');
  END IF;
  IF to_regnamespace('geonaf_legacy_draft') IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: le schema geonaf_legacy_draft existe deja';
  END IF;
  SELECT string_agg(relname, ',' ORDER BY relname) INTO found FROM pg_class
   WHERE relnamespace = legacy AND relkind IN ('r', 'p');
  IF found IS DISTINCT FROM expected THEN
    RAISE EXCEPTION 'GEONAF: tables inattendues dans geonaf' USING DETAIL = coalesce(found, 'aucune');
  END IF;
  IF NOT (SELECT pg_has_role(current_user, nspowner, 'MEMBER') FROM pg_namespace WHERE oid = legacy) THEN
    RAISE EXCEPTION 'GEONAF: le role courant n''est pas proprietaire du schema geonaf';
  END IF;

  FOR t IN SELECT relname, relowner FROM pg_class WHERE relnamespace = legacy AND relkind IN ('r', 'p') LOOP
    IF NOT (pg_has_role(current_user, t.relowner, 'USAGE')
            OR (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)) THEN
      blockers := blockers || ('comptage filtre par la RLS sur ' || t.relname);
    END IF;
    EXECUTE 'SELECT count(*) FROM geonaf.' || quote_ident(t.relname) INTO row_count;
    IF row_count > 0 THEN
      blockers := blockers || (row_count || ' lignes dans ' || t.relname);
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = legacy AND relkind IN ('v', 'm', 'f', 'c')) THEN
    blockers := blockers || 'vue, table etrangere ou type composite dans geonaf'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = legacy) THEN
    blockers := blockers || 'fonction dans geonaf'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
             WHERE c.relnamespace = legacy AND NOT g.tgisinternal) THEN
    blockers := blockers || 'declencheur sur une table de geonaf'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_depend d
             JOIN pg_rewrite r ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
             JOIN pg_class v ON v.oid = r.ev_class
             JOIN pg_class s ON d.refclassid = 'pg_class'::regclass AND s.oid = d.refobjid
             WHERE s.relnamespace = legacy AND v.relnamespace <> legacy) THEN
    blockers := blockers || 'vue hors geonaf qui lit geonaf'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class s ON s.oid = k.confrelid
             WHERE k.contype = 'f' AND s.relnamespace = legacy AND k.connamespace <> legacy) THEN
    blockers := blockers || 'cle etrangere hors geonaf vers geonaf'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace <> legacy
             AND pronamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
             AND strpos(coalesce(prosrc, ''), 'geonaf') > 0) THEN
    blockers := blockers || 'fonction hors geonaf qui cite geonaf'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_db_role_setting, unnest(setconfig) AS setting WHERE strpos(setting, 'geonaf') > 0) THEN
    blockers := blockers || 'parametre de role ou de base qui cite geonaf (search_path ou Data API)'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_publication_tables WHERE schemaname = 'geonaf') THEN
    blockers := blockers || 'publication contenant une table de geonaf'::text;
  END IF;
  IF to_regclass('cron.job') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM cron.job WHERE strpos(command, ''geonaf'') > 0' INTO row_count;
    IF row_count > 0 THEN
      blockers := blockers || 'tache pg_cron qui cite geonaf'::text;
    END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extnamespace = legacy) THEN
    blockers := blockers || 'extension installee dans geonaf'::text;
  END IF;
  IF cardinality(blockers) > 0 THEN
    RAISE EXCEPTION 'GEONAF: renommage refuse' USING DETAIL = array_to_string(blockers, ' ; ');
  END IF;
END $$;

ALTER SCHEMA geonaf RENAME TO geonaf_legacy_draft;
COMMENT ON SCHEMA geonaf_legacy_draft IS 'geonaf-legacy: schema herite trouve le 2026-11-03, renomme avant la migration 0001, non utilise';

-- ===== Result =====
-- Simulation: the deliberate error below cancels the whole request; nothing is kept.
DO $$
BEGIN
  RAISE EXCEPTION 'GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE';
END $$;
