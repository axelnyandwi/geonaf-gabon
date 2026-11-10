-- GEONAF - Supabase SQL Editor - 09 ROLLBACK of the legacy schema rename. REQUIRES EXPLICIT VALIDATION.
--
-- Use only after 09 application (or after an accidental rename), and only while migration 0001 has not
-- been applied: a schema named geonaf must not exist. Gives the schema back its name (same OID, so every
-- table, grant, policy and row stays as it was) and its exact initial comment, read from the mark that
-- 09 wrote: mark prefix, separator, then the initial comment in JSON (null or a string). Nothing is
-- assumed: a mark that is not exactly the one written by 09 stops the script, and a manual decision is needed.
--
-- CONFIRMATION REQUIRED: this file refuses to run as committed. Replace AAAA-MM-JJ HH:MM:SS.UUUUUU below
-- by the value of line -3 of 08 (instance start, UTC, with microseconds) run just before on the same project.
-- Safety (D2.9): a single DO statement plus a read-only SELECT; the rename and the comment exist only as
-- format() templates, so a partial selection is a syntax error or the read-only SELECT.
-- Expected after: 09_legacy_schema_fingerprint.sql returns the values exported before 09 application.
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
  draft oid := to_regnamespace(new_name);
  current_mark text;
  initial_json jsonb;
  initial_comment text;
  found_tables text;
  t record;
BEGIN
  IF confirmation IS DISTINCT FROM to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') THEN
    RAISE EXCEPTION 'GEONAF: RETOUR ARRIERE REFUSE, confirmation absente ou differente de cette instance'
      USING HINT = 'Remplacer AAAA-MM-JJ HH:MM:SS.UUUUUU par la ligne -3 de 08 executee juste avant sur ce projet.';
  END IF;
  IF to_regnamespace(old_name) IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: un schema geonaf existe (migration 0001 appliquee ?), retour arriere refuse';
  END IF;
  IF draft IS NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf_legacy_draft absent, rien a retablir';
  END IF;
  current_mark := obj_description(draft, 'pg_namespace');
  IF current_mark IS NULL OR left(current_mark, length(mark_prefix || initial_separator)) <> mark_prefix || initial_separator THEN
    RAISE EXCEPTION 'GEONAF: marque inattendue sur geonaf_legacy_draft, retour arriere refuse'
      USING DETAIL = coalesce(current_mark, '(aucune)');
  END IF;
  -- An invalid JSON makes the cast fail: the request stops and nothing is changed.
  initial_json := substr(current_mark, length(mark_prefix || initial_separator) + 1)::jsonb;
  -- PostgreSQL never stores an empty comment (an empty one removes it): "" can only be a forged mark.
  IF jsonb_typeof(initial_json) NOT IN ('null', 'string') OR initial_json = '""'::jsonb THEN
    RAISE EXCEPTION 'GEONAF: commentaire initial illisible dans la marque' USING DETAIL = current_mark;
  END IF;
  initial_comment := initial_json #>> '{}';      -- NULL for JSON null, the exact text for a JSON string.
  IF NOT (SELECT pg_has_role(current_user, nspowner, 'USAGE') FROM pg_namespace WHERE oid = draft)
     OR NOT has_database_privilege(current_database(), 'CREATE') THEN
    RAISE EXCEPTION 'GEONAF: le role courant n''a pas les droits du proprietaire du schema ou CREATE sur la base';
  END IF;
  FOR t IN SELECT relname FROM pg_class WHERE relnamespace = draft AND relkind IN ('r', 'p') LOOP
    EXECUTE format('LOCK TABLE %I.%I IN SHARE ROW EXCLUSIVE MODE NOWAIT', new_name, t.relname);
  END LOOP;
  found_tables := (SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class
                   WHERE relnamespace = draft AND relkind IN ('r', 'p'));
  IF found_tables IS DISTINCT FROM expected THEN
    RAISE EXCEPTION 'GEONAF: tables inattendues dans geonaf_legacy_draft, retour arriere refuse'
      USING DETAIL = coalesce(found_tables, 'aucune');
  END IF;

  EXECUTE format('ALTER SCHEMA %I RENAME TO %I', new_name, old_name);
  IF initial_comment IS NULL THEN
    EXECUTE format('COMMENT ON SCHEMA %I IS NULL', old_name);
  ELSE
    EXECUTE format('COMMENT ON SCHEMA %I IS %L', old_name, initial_comment);
  END IF;
  IF to_regnamespace(old_name) IS DISTINCT FROM draft OR to_regnamespace(new_name) IS NOT NULL
     OR obj_description(draft, 'pg_namespace') IS DISTINCT FROM initial_comment THEN
    RAISE EXCEPTION 'GEONAF: retour arriere incoherent, tout est annule';
  END IF;
END $$;

SELECT to_regnamespace('geonaf') IS NOT NULL AS geonaf_retabli,
       to_regnamespace('geonaf_legacy_draft') IS NULL AS geonaf_legacy_draft_libere,
       (SELECT count(*) FROM pg_class WHERE relnamespace = to_regnamespace('geonaf') AND relkind = 'r') AS tables,
       coalesce(obj_description(to_regnamespace('geonaf'), 'pg_namespace'), '(aucun)') AS commentaire;
