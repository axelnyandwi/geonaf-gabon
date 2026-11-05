-- GEONAF - Supabase SQL Editor - 09 rename of the legacy geonaf schema: SIMULATION. Writes nothing.
--
-- Paste the whole file in a new query, select nothing, Run. Expected outcome: the error
-- GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE. Any other message: stop.
-- Refuses to run unless the schema is exactly the legacy one found on 2026-11-03 (8 known tables, empty,
-- no comment) and nothing outside it refers to it (same blocking checks as 08).
--
-- Safety (D2.9), checked by tests/test_supabase_sql.py and tests/test_supabase_legacy_rename_integration.py:
-- - The file holds a single DO statement. A partial selection is a syntax error and runs nothing; no
--   selection of any line range can rename, comment or create anything.
-- - The rename statements exist only as format() templates built at run time: no text of this file,
--   comments included, is a runnable rename or comment statement.
-- - The rename runs inside a PL/pgSQL sub-block (a subtransaction) that is always rolled back by the
--   internal signal GN009; the block then checks that the schema has its initial name, OID and comment,
--   and the final error cancels the whole request (second, independent cancellation).
-- - What PostgreSQL cannot cancel: sequence increments made by event triggers (pg_graphql bumps
--   graphql.seq_schema_version on DDL), statistics and logs. They do not touch geonaf. The success
--   message lists the event triggers that fire on this DDL; a mark (*) flags a possible non
--   transactional effect.
-- Before and after: 09_legacy_schema_fingerprint.sql must return the same values (docs/supabase_security_d2_2.md).
DO $$
DECLARE
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
  event_triggers text;
  rename_tested boolean := false;     -- Set just before GN009: proves the signal is ours (D2.9 review).
BEGIN
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
             AND (strpos(coalesce(prosrc, ''), old_name) > 0
                  OR strpos(coalesce(array_to_string(proconfig, ' '), ''), old_name) > 0)) THEN
    -- proconfig: SET search_path = geonaf would bind the function to the NEW geonaf of 0001 (D2.9 audit).
    blockers := blockers || 'fonction hors geonaf qui cite geonaf (code ou search_path)'::text;
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

  -- ===== Simulation: rename in a subtransaction that is always rolled back =====
  BEGIN
    EXECUTE format('ALTER SCHEMA %I RENAME TO %I', old_name, new_name);
    EXECUTE format('COMMENT ON SCHEMA %I IS %L', new_name, mark);
    IF to_regnamespace(new_name) IS DISTINCT FROM legacy
       OR obj_description(legacy, 'pg_namespace') IS DISTINCT FROM mark THEN
      RAISE EXCEPTION 'GEONAF: renommage simule incoherent';
    END IF;
    rename_tested := true;
    RAISE EXCEPTION USING ERRCODE = 'GN009', MESSAGE = 'GEONAF: annulation interne du renommage simule';
  EXCEPTION WHEN SQLSTATE 'GN009' THEN
    NULL;    -- Only the internal signal is caught: any real error of the rename stops the request.
  END;
  -- PL/pgSQL keeps local variables after a caught error: false here means GN009 was raised by someone
  -- else (an event trigger, for example) before the rename and its checks ran.
  IF NOT rename_tested THEN
    RAISE EXCEPTION 'GEONAF: signal GN009 inattendu, simulation non concluante, ne pas appliquer 09';
  END IF;
  IF to_regnamespace(old_name) IS DISTINCT FROM legacy OR to_regnamespace(new_name) IS NOT NULL
     OR obj_description(legacy, 'pg_namespace') IS DISTINCT FROM initial_comment THEN
    RAISE EXCEPTION 'GEONAF: ETAT NON RESTAURE APRES LA SIMULATION, ne rien relancer';
  END IF;
  event_triggers := (SELECT string_agg(e.evtname || ':' || e.evtfoid::regproc::text
                                       || CASE WHEN p.prosrc ~* '(nextval|setval|dblink|pg_background|http|net\.)'
                                               THEN '(*)' ELSE '' END, ', ' ORDER BY e.evtname)
                     FROM pg_event_trigger e JOIN pg_proc p ON p.oid = e.evtfoid
                     WHERE e.evtenabled <> 'D' AND e.evtevent IN ('ddl_command_start', 'ddl_command_end')
                     AND (e.evttags IS NULL OR e.evttags && ARRAY['ALTER SCHEMA', 'COMMENT']));
  RAISE EXCEPTION 'GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE'
    USING DETAIL = 'declencheurs d''evenement executes : ' || coalesce(event_triggers, 'aucun');
END $$;
