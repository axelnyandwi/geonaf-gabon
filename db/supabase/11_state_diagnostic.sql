-- GEONAF - Supabase SQL Editor - 11 state diagnostic of one project (read only: one SELECT).
--
-- Answers "where is this project in the D2 sequence?" without assuming anything: schemas present,
-- tables and their structure, migrations recorded, PostGIS, GEONAF roles, grants to the API roles,
-- outside dependencies, event triggers, environment mark, then the recognised state and the next
-- step. Deep checks stay in the existing scripts named by the last line (08, 10, 04): nothing here
-- replaces them.
--
-- Run the whole file in a new query, on geonaf-test then geonaf-dev; export each result as CSV
-- (geonaf-<project>_11_<AAAAMMJJ>.csv, outside the repository and outside OneDrive). Compare the two
-- exports offline: python scripts/compare_state_diagnostics.py <test.csv> <dev.csv>. Rows with
-- comparable = true must be identical between the two projects when they are in the same state.
-- Row counts are statistics (n_live_tup), not exact counts: 08 line 5 counts exactly.
-- Writes nothing: the first statement makes the whole request a read-only transaction; the search
-- path is empty so that every name is printed qualified, the same way in both projects.
SELECT set_config('transaction_read_only', 'on', true);
SELECT set_config('search_path', '', true);

WITH ns AS (
  SELECT n.oid, n.nspname, n.nspowner::regrole::text AS owner, obj_description(n.oid, 'pg_namespace') AS note
  FROM pg_catalog.pg_namespace n WHERE n.nspname IN ('geonaf', 'geonaf_legacy_draft')
), tbl AS (
  SELECT c.oid, s.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity
  FROM pg_catalog.pg_class c JOIN ns s ON s.oid = c.relnamespace WHERE c.relkind IN ('r', 'p')
), columns_by_schema AS (
  SELECT t.nspname, string_agg(t.relname || '(' || coalesce((
           SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
                             || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END, ', ' ORDER BY a.attnum)
           FROM pg_catalog.pg_attribute a WHERE a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped), '')
         || ')', E'\n' ORDER BY t.relname) AS text
  FROM tbl t GROUP BY t.nspname
), constraints_by_schema AS (
  SELECT s.nspname, string_agg(k.conname || ' ' || k.contype::text || ' '
           || replace(pg_get_constraintdef(k.oid), s.nspname || '.', ''), E'\n' ORDER BY k.conname) AS text
  FROM pg_catalog.pg_constraint k JOIN ns s ON s.oid = k.connamespace GROUP BY s.nspname
), facts AS (
  SELECT
    (SELECT note FROM ns WHERE nspname = 'geonaf') AS geonaf_note,
    (SELECT note FROM ns WHERE nspname = 'geonaf_legacy_draft') AS legacy_note,
    EXISTS (SELECT 1 FROM ns WHERE nspname = 'geonaf') AS has_geonaf,
    EXISTS (SELECT 1 FROM ns WHERE nspname = 'geonaf_legacy_draft') AS has_legacy,
    (SELECT string_agg(relname, ',' ORDER BY relname) FROM tbl WHERE nspname = 'geonaf') AS geonaf_tables,
    -- Tables that exist only after the migrations (the legacy schema also has territory_versions and
    -- indicator_results: never use them to recognise a migration).
    to_regclass('geonaf.source_releases') IS NOT NULL AS has_0001,
    to_regclass('geonaf.acquisition_manifests') IS NOT NULL AS has_0002,
    (SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname IN ('geonaf_reader', 'geonaf_importer')) AS geonaf_roles,
    (SELECT extversion FROM pg_catalog.pg_extension WHERE extname = 'postgis') AS postgis
), state AS (
  SELECT f.*, CASE
    WHEN f.has_geonaf AND NOT f.has_0001 AND f.geonaf_note IS NULL AND NOT f.has_legacy
         AND f.geonaf_tables = 'acquisitions,analysis_acquisitions,analysis_runs,indicator_definitions,indicator_results,sources,territories,territory_versions'
      THEN 'S0'
    WHEN NOT f.has_geonaf AND f.has_legacy AND left(coalesce(f.legacy_note, ''), 14) = 'geonaf-legacy:'
      THEN 'S1'
    WHEN f.has_0001 AND NOT f.has_0002 AND f.geonaf_note IS NULL THEN 'S3'
    WHEN f.has_0001 AND NOT f.has_0002 AND f.geonaf_note IN ('geonaf-environment:dev', 'geonaf-environment:test')
      THEN 'S4'
    WHEN f.has_0001 AND f.has_0002 AND f.geonaf_note IN ('geonaf-environment:dev', 'geonaf-environment:test')
      THEN 'S5'
    WHEN NOT f.has_geonaf AND NOT f.has_legacy THEN 'V'
    ELSE 'INCOHERENT' END AS code
  FROM facts f
)
SELECT ordre, rubrique, element, valeur, comparable FROM (
  SELECT 0 AS ordre, 'execution' AS rubrique, 'date UTC' AS element,
         to_char(transaction_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS valeur, false AS comparable
  UNION ALL SELECT 0, 'execution', 'demarrage instance UTC',
         to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US'), false
  UNION ALL SELECT 0, 'execution', 'lecture seule', current_setting('transaction_read_only'), true
  UNION ALL SELECT 1, 'serveur', 'version majeure PostgreSQL', (current_setting('server_version_num')::integer / 10000)::text, true
  UNION ALL SELECT 2, 'extension', e.extname, e.extversion || ' dans ' || n.nspname, true
    FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname IN ('postgis', 'postgis_topology', 'postgis_raster', 'pg_graphql', 'pg_cron', 'pg_net', 'supautils')
  UNION ALL SELECT 2, 'extension', 'postgis', 'absente', true FROM facts WHERE postgis IS NULL
  UNION ALL SELECT 3, 'schema', s.name, CASE WHEN n.oid IS NULL THEN 'absent' ELSE 'present, proprietaire ' || n.owner END, true
    FROM (VALUES ('geonaf'), ('geonaf_legacy_draft')) AS s(name) LEFT JOIN ns n ON n.nspname = s.name
  UNION ALL SELECT 3, 'schema', n.nspname || ' : oid', n.oid::text, false FROM ns n
  UNION ALL SELECT 3, 'schema', n.nspname || ' : commentaire', coalesce(to_jsonb(n.note)::text, 'null'),
         n.nspname = 'geonaf_legacy_draft' OR left(coalesce(n.note, ''), 19) <> 'geonaf-environment:'
    FROM ns n
  UNION ALL SELECT 4, 'tables', n.nspname, coalesce((SELECT string_agg(relname, ',' ORDER BY relname) FROM tbl t
                                                     WHERE t.nspname = n.nspname), 'aucune'), true FROM ns n
  UNION ALL SELECT 4, 'tables', n.nspname || ' : structure sha256',
         encode(sha256(convert_to(coalesce((SELECT text FROM columns_by_schema c WHERE c.nspname = n.nspname), '') || E'\n#\n'
                                  || coalesce((SELECT text FROM constraints_by_schema k WHERE k.nspname = n.nspname), ''),
                                  'UTF8')), 'hex'), true FROM ns n
  UNION ALL SELECT 4, 'tables', n.nspname || ' : lignes estimees (statistiques)',
         coalesce((SELECT sum(st.n_live_tup)::text FROM pg_catalog.pg_stat_user_tables st WHERE st.schemaname = n.nspname), '0'),
         false FROM ns n
  UNION ALL SELECT 4, 'tables', n.nspname || ' : tables sans RLS',
         (SELECT count(*)::text FROM tbl t WHERE t.nspname = n.nspname AND NOT t.relrowsecurity), true FROM ns n
  UNION ALL SELECT 5, 'migrations', 'geonaf.schema_migrations',
         CASE WHEN to_regclass('geonaf.schema_migrations') IS NULL THEN 'absente'
              ELSE btrim(regexp_replace(regexp_replace(query_to_xml(
                     'SELECT string_agg(version || '' '' || body_sha256 || '' '' || channel, '' ; '' ORDER BY version) AS v '
                     || 'FROM geonaf.schema_migrations', false, true, '')::text, '<[^>]*>', '', 'g'), '\s+', ' ', 'g')) END,
         true
  UNION ALL SELECT 5, 'migrations', 'public.alembic_version',
         CASE WHEN to_regclass('public.alembic_version') IS NULL THEN 'absente'
              ELSE btrim(regexp_replace(regexp_replace(query_to_xml(
                     'SELECT string_agg(version_num, '','' ORDER BY version_num) AS v FROM public.alembic_version',
                     false, true, '')::text, '<[^>]*>', '', 'g'), '\s+', ' ', 'g')) END,
         true
  UNION ALL SELECT 6, 'roles', r.name,
         coalesce((SELECT 'login=' || rolcanlogin::text || ' super=' || rolsuper::text || ' bypassrls=' || rolbypassrls::text
                   FROM pg_catalog.pg_roles WHERE rolname = r.name), 'absent'), true
    FROM (VALUES ('geonaf_reader'), ('geonaf_importer'), ('geonaf_migrator')) AS r(name)
  UNION ALL SELECT 7, 'droits', 'objets GEONAF accessibles a anon, authenticated, service_role ou PUBLIC',
         -- CASE fixes the order: the privilege function is never called for a role that does not exist.
         (SELECT count(*)::text FROM tbl t, LATERAL (VALUES ('anon'), ('authenticated'), ('service_role')) AS a(role)
          WHERE CASE WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = a.role)
                     THEN has_table_privilege(a.role, t.oid, 'SELECT,INSERT,UPDATE,DELETE') ELSE false END)
         || ' tables ; ' || (SELECT count(*)::text FROM ns n, LATERAL (VALUES ('anon'), ('authenticated'), ('service_role')) AS a(role)
          WHERE CASE WHEN EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = a.role)
                     THEN has_schema_privilege(a.role, n.oid, 'USAGE') ELSE false END)
         || ' schemas ; PUBLIC ' || (SELECT count(*)::text FROM ns n WHERE has_schema_privilege('public', n.oid, 'USAGE')),
         true
  -- Effective PostGIS privileges (USAGE on its schema, EXECUTE on the ST_* functions GEONAF calls, no CREATE).
  -- CASE fixes the order: the privilege functions are never called for an absent role or extension.
  UNION ALL SELECT 7, 'droits', r.name || ' : PostGIS (usage, execute ST_*, create)',
         CASE WHEN NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = r.name) THEN 'role absent'
              WHEN NOT EXISTS (SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'postgis') THEN 'PostGIS absent'
              ELSE (SELECT 'usage=' || has_schema_privilege(r.name, e.extnamespace, 'USAGE')::text
                           || ' execute=' || coalesce(bool_and(has_function_privilege(r.name, p.oid, 'EXECUTE')),
                                                      false)::text
                           || ' create=' || has_schema_privilege(r.name, e.extnamespace, 'CREATE')::text
                    FROM pg_catalog.pg_extension e
                    LEFT JOIN pg_catalog.pg_proc p ON p.pronamespace = e.extnamespace
                         AND p.proname IN ('st_intersects', 'st_makeenvelope', 'st_asbinary', 'st_geomfromwkb')
                    WHERE e.extname = 'postgis' GROUP BY e.extnamespace) END,
         true
    FROM (VALUES ('geonaf_reader'), ('geonaf_importer')) AS r(name)
  UNION ALL SELECT 8, 'dependances', 'vues hors des schemas GEONAF qui les lisent',
         (SELECT count(DISTINCT v.oid)::text FROM pg_catalog.pg_depend d
            JOIN pg_catalog.pg_rewrite w ON d.classid = 'pg_catalog.pg_rewrite'::regclass AND d.objid = w.oid
            JOIN pg_catalog.pg_class v ON v.oid = w.ev_class
            JOIN pg_catalog.pg_class s ON d.refclassid = 'pg_catalog.pg_class'::regclass AND s.oid = d.refobjid
          WHERE s.relnamespace IN (SELECT oid FROM ns) AND v.relnamespace NOT IN (SELECT oid FROM ns)), true
  UNION ALL SELECT 8, 'dependances', 'cles etrangeres hors des schemas GEONAF vers eux',
         (SELECT count(*)::text FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class s ON s.oid = k.confrelid
          WHERE k.contype = 'f' AND s.relnamespace IN (SELECT oid FROM ns) AND k.connamespace NOT IN (SELECT oid FROM ns)), true
  UNION ALL SELECT 8, 'dependances', 'fonctions hors catalogue qui citent geonaf (code ou search_path)',
         (SELECT count(*)::text FROM pg_catalog.pg_proc
          WHERE pronamespace NOT IN (SELECT oid FROM ns)
            AND pronamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
            AND (strpos(coalesce(prosrc, ''), 'geonaf') > 0
                 OR strpos(coalesce(array_to_string(proconfig, ' '), ''), 'geonaf') > 0)), true
  UNION ALL SELECT 8, 'dependances', 'parametres de role ou de base qui citent geonaf',
         (SELECT count(*)::text FROM pg_catalog.pg_db_role_setting, unnest(setconfig) AS setting
          WHERE strpos(setting, 'geonaf') > 0), true
  UNION ALL SELECT 8, 'dependances', 'publications contenant une table GEONAF',
         (SELECT count(*)::text FROM pg_catalog.pg_publication_tables
          WHERE schemaname IN ('geonaf', 'geonaf_legacy_draft')), true
  UNION ALL SELECT 9, 'declencheurs d''evenement', coalesce(e.evtname, 'aucun'),
         coalesce(e.evtevent || ' ' || e.evtenabled::text || ' ' || e.evtfoid::regproc::text, ''), true
    FROM (SELECT 1) AS one LEFT JOIN pg_catalog.pg_event_trigger e ON true
  UNION ALL SELECT 98, 'etat', 'etat reconnu', s.code || ' : ' || CASE s.code
         WHEN 'S0' THEN 'schema herite geonaf en place, 09 non applique'
         WHEN 'S1' THEN 'schema herite renomme en geonaf_legacy_draft, migration 0001 non appliquee'
         WHEN 'S3' THEN 'migration 0001 appliquee, environnement non marque'
         WHEN 'S4' THEN 'migration 0001 appliquee et marquee (' || s.geonaf_note || '), 0002 non appliquee'
         WHEN 'S5' THEN 'migrations 0001 et 0002 appliquees (' || s.geonaf_note || ')'
         WHEN 'V' THEN 'aucun schema GEONAF'
         ELSE 'combinaison non prevue : ne rien executer, transmettre cet export' END
         || CASE WHEN s.has_legacy AND s.code IN ('S3', 'S4', 'S5') THEN ' ; geonaf_legacy_draft conserve' ELSE '' END,
         true FROM state s
  UNION ALL SELECT 99, 'etat', 'prochaine etape', CASE s.code
         WHEN 'S0' THEN '08 puis simulation 09 (docs/supabase_security_d2_2.md section 6), puis 09 application apres autorisation'
         WHEN 'S1' THEN '10_verify_legacy_schema_rename (GO), 00_preflight, '
              || CASE WHEN s.geonaf_roles = 2 THEN '' ELSE '01_roles apres autorisation, ' END
              || '10_security_precheck et controles S2-S8, simulation 02, puis 02 application apres autorisation'
         WHEN 'S3' THEN '03_mark_environment_<test|dev> apres autorisation, puis 04_verify_schema'
         WHEN 'S4' THEN '04_verify_schema, import du registre (05/06), puis simulation 12 (migration 0002)'
         WHEN 'S5' THEN 'import des observations D3 apres autorisation (docs/postgis_storage_d3_d4.md)'
         WHEN 'V' THEN 'aucune : verifier le projet (mauvais projet ?)'
         ELSE 'aucune : decision manuelle' END
         || CASE WHEN s.postgis IS NULL AND s.code IN ('S1', 'V') THEN ' ; PostGIS a activer par un administrateur' ELSE '' END,
         true FROM state s
) AS d
ORDER BY ordre, rubrique, element;
