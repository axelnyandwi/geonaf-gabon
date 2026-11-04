-- GEONAF - Supabase SQL Editor - 09 rename of the legacy geonaf schema: APPLICATION. REQUIRES EXPLICIT VALIDATION.
--
-- Renames the legacy schema and marks it; non destructive: tables, columns, constraints, indexes,
-- sequences, grants, policies, comments and rows are kept (PostgreSQL addresses them by OID).
-- Refuses to run unless the schema is exactly the legacy one found on 2026-11-03 (8 known tables, empty,
-- no comment) and nothing outside it refers to it (same guard as the simulation, character for character).
--
-- CONFIRMATION REQUIRED: this file refuses to run as committed. Replace AAAA-MM-JJ HH:MM:SS.UUUUUU below
-- by the value of line -3 of 08 (instance start, UTC, with microseconds) run just before on the same
-- project. A copy of the wrong file, or a run on another instance, then stops before any check.
--
-- Safety (D2.9): the rename and the comment run inside the single DO statement, after the guard, and exist
-- only as format() templates; a partial selection is a syntax error or the final read-only SELECT.
-- Undo: 09_rename_legacy_schema_rollback.sql (restores the name and the exact initial comment kept in the mark).
DO $$
DECLARE
  -- ===== Confirmation (application and rollback only) =====
  confirmation constant text := 'AAAA-MM-JJ HH:MM:SS.UUUUUU';
  -- ===== End of confirmation =====
  -- ===== Constants (identical in simulation, application and rollback) =====
  old_name constant text := 'geonaf';
  new_name constant text := 'geonaf_legacy_draft';
  mark_prefix constant text := 'geonaf-legacy: schema herite trouve le 2026-11-03, renomme avant la migration 0001, non utilise';
  initial_separator constant text := ' ; commentaire initial (JSON) : ';
  expected constant text := 'acquisitions,analysis_acquisitions,analysis_runs,indicator_definitions,indicator_results,sources,territories,territory_versions';
  -- ===== End of constants =====
  -- ===== Guard variables (identical in simulation and application) =====
  legacy oid := to_regnamespace(old_name);
  initial_comment text;
  mark text;
  found_tables text;
  n_rows bigint;
  blockers text[] := '{}';
  t record;
  -- ===== End of guard variables =====
BEGIN
  IF confirmation IS DISTINCT FROM to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') THEN
    RAISE EXCEPTION 'GEONAF: APPLICATION REFUSEE, confirmation absente ou differente de cette instance'
      USING HINT = 'Remplacer AAAA-MM-JJ HH:MM:SS.UUUUUU par la ligne -3 de 08 executee juste avant sur ce projet.';
  END IF;

  -- ===== Guard (identical in simulation and application) =====
  IF legacy IS NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf absent, rien a renommer';
  END IF;
  initial_comment := obj_description(legacy, 'pg_namespace');
  IF initial_comment IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf marque, ce n''est pas le schema herite'
      USING DETAIL = initial_comment;
  END IF;
  IF to_regnamespace(new_name) IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: le schema geonaf_legacy_draft existe deja';
  END IF;
  found_tables := (SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class
                   WHERE relnamespace = legacy AND relkind IN ('r', 'p'));
  IF found_tables IS DISTINCT FROM expected THEN
    RAISE EXCEPTION 'GEONAF: tables inattendues dans geonaf' USING DETAIL = coalesce(found_tables, 'aucune');
  END IF;
  IF NOT (SELECT pg_has_role(current_user, nspowner, 'USAGE') FROM pg_namespace WHERE oid = legacy)
     OR NOT has_database_privilege(current_database(), 'CREATE') THEN
    RAISE EXCEPTION 'GEONAF: le role courant n''a pas les droits du proprietaire du schema geonaf ou CREATE sur la base';
  END IF;

  -- The 8 tables are locked until the end of the request: between these checks and the rename nobody can
  -- write into them nor add a trigger or a foreign key to them (a new view is not prevented; the risk is
  -- accepted, the window lasts milliseconds). NOWAIT: fails at once if another session holds a lock.
  FOR t IN SELECT relname FROM pg_class WHERE relnamespace = legacy AND relkind IN ('r', 'p') LOOP
    EXECUTE format('LOCK TABLE %I.%I IN SHARE ROW EXCLUSIVE MODE NOWAIT', old_name, t.relname);
  END LOOP;
  -- Read again under the locks: a table created between the first read and the locks is seen here.
  found_tables := (SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class
                   WHERE relnamespace = legacy AND relkind IN ('r', 'p'));
  IF found_tables IS DISTINCT FROM expected THEN
    RAISE EXCEPTION 'GEONAF: tables inattendues dans geonaf' USING DETAIL = coalesce(found_tables, 'aucune');
  END IF;

  FOR t IN SELECT relname, relowner, relforcerowsecurity FROM pg_class
           WHERE relnamespace = legacy AND relkind IN ('r', 'p') LOOP
    IF NOT ((pg_has_role(current_user, t.relowner, 'USAGE') AND NOT t.relforcerowsecurity)
            OR (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)) THEN
      blockers := blockers || ('comptage filtre par la RLS sur ' || t.relname);
    END IF;
    EXECUTE format('SELECT count(*) FROM %I.%I', old_name, t.relname) INTO n_rows;
    IF n_rows > 0 THEN
      blockers := blockers || (n_rows || ' lignes dans ' || t.relname);
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
             AND strpos(coalesce(prosrc, ''), old_name) > 0) THEN
    blockers := blockers || 'fonction hors geonaf qui cite geonaf'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_db_role_setting, unnest(setconfig) AS setting WHERE strpos(setting, old_name) > 0) THEN
    blockers := blockers || 'parametre de role ou de base qui cite geonaf (search_path ou Data API)'::text;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_publication_tables WHERE schemaname = old_name) THEN
    blockers := blockers || 'publication contenant une table de geonaf'::text;
  END IF;
  IF to_regclass('cron.job') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM cron.job WHERE strpos(command, $1) > 0' INTO n_rows USING old_name;
    IF n_rows > 0 THEN
      blockers := blockers || 'tache pg_cron qui cite geonaf'::text;
    END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extnamespace = legacy) THEN
    blockers := blockers || 'extension installee dans geonaf'::text;
  END IF;
  IF cardinality(blockers) > 0 THEN
    RAISE EXCEPTION 'GEONAF: renommage refuse' USING DETAIL = array_to_string(blockers, ' ; ');
  END IF;
  -- The mark keeps the exact initial comment (JSON: null or a string) for the rollback script.
  mark := mark_prefix || initial_separator || coalesce(to_jsonb(initial_comment)::text, 'null');
  -- ===== End of guard =====

  -- ===== Application =====
  EXECUTE format('ALTER SCHEMA %I RENAME TO %I', old_name, new_name);
  EXECUTE format('COMMENT ON SCHEMA %I IS %L', new_name, mark);
  IF to_regnamespace(new_name) IS DISTINCT FROM legacy OR to_regnamespace(old_name) IS NOT NULL
     OR obj_description(legacy, 'pg_namespace') IS DISTINCT FROM mark THEN
    RAISE EXCEPTION 'GEONAF: renommage incoherent, tout est annule';
  END IF;
END $$;

SELECT to_regnamespace('geonaf') IS NULL AS geonaf_libere,
       (SELECT count(*) FROM pg_class WHERE relnamespace = to_regnamespace('geonaf_legacy_draft')
        AND relkind = 'r') AS tables_conservees,
       obj_description(to_regnamespace('geonaf_legacy_draft'), 'pg_namespace') AS marque;
