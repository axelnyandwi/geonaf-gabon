-- GEONAF - Supabase SQL Editor - 08 inventory of the legacy geonaf schema before its rename (read only: one SELECT).
--
-- The geonaf schema found on geonaf-dev on 2026-11-03 is not migration 0001: 8 tables (acquisitions,
-- analysis_*, indicator_*, sources, territories, territory_versions), no environment mark, no policy,
-- no trigger. Option A (validated): rename it, then apply migration 0001 (docs/supabase_legacy_schema_plan.md).
-- Run on each project BEFORE 09. Every line must read conforme = true, except the informative lines
-- (attendu = 'information'). A false line blocks the rename; 09 repeats the blocking checks and refuses.
-- Writes nothing: the first statement makes the whole request a read-only transaction, so any write
-- attempt fails. Row counts read the geonaf tables only; cron jobs are read only if pg_cron is installed.
-- Lines 0a-0c identify the server: export one CSV per project, named after the project shown in the
-- dashboard; the fingerprints of geonaf-test and geonaf-dev must differ.
SELECT set_config('transaction_read_only', 'on', true);

WITH legacy AS (
  SELECT c.oid, c.relname, c.relkind, c.relowner, c.relforcerowsecurity FROM pg_class c
  WHERE c.relnamespace = to_regnamespace('geonaf')
), legacy_tables AS (
  SELECT * FROM legacy WHERE relkind IN ('r', 'p')
)
SELECT verification, attendu, obtenu,
       CASE WHEN attendu = 'information' THEN NULL ELSE attendu = obtenu END AS conforme
FROM (
  SELECT -3 AS ordre, 'transaction en lecture seule' AS verification, 'on' AS attendu,
         current_setting('transaction_read_only') AS obtenu
  UNION ALL SELECT -2, 'empreinte du serveur (12 premiers caracteres du md5 de l''identifiant systeme)', 'information',
         CASE WHEN has_function_privilege('pg_control_system()', 'EXECUTE')
              THEN left(md5((xpath('/row/s/text()', query_to_xml(
                     'SELECT system_identifier AS s FROM pg_control_system()', false, true, '')))[1]::text), 12)
              ELSE 'non disponible (droit EXECUTE absent)' END
  UNION ALL SELECT -1, 'demarrage du serveur et version', 'information',
         pg_postmaster_start_time()::text || ' ; ' || current_setting('server_version')
  UNION ALL SELECT 1, 'schema geonaf present', 'true',
         (to_regnamespace('geonaf') IS NOT NULL)::text
  UNION ALL SELECT 2, 'marque d''environnement (aucune pour le schema herite)', '(aucune)',
         coalesce(obj_description(to_regnamespace('geonaf'), 'pg_namespace'), '(aucune)')
  UNION ALL SELECT 3, 'tables du schema herite',
         'acquisitions,analysis_acquisitions,analysis_runs,indicator_definitions,indicator_results,sources,territories,territory_versions',
         coalesce((SELECT string_agg(relname, ',' ORDER BY relname) FROM legacy_tables), '')
  UNION ALL SELECT 4, 'comptage sans filtrage RLS (BYPASSRLS, ou proprietaire sans FORCE RLS)', 'true',
         coalesce((SELECT bool_and((pg_has_role(current_user, relowner, 'USAGE') AND NOT relforcerowsecurity)
                                   OR (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user))
                   FROM legacy_tables), false)::text
  UNION ALL SELECT 5, 'lignes dans les tables du schema herite', '0',
         (SELECT coalesce(sum((xpath('/row/n/text()', query_to_xml(
                   'SELECT count(*) AS n FROM geonaf.' || quote_ident(relname), false, true, '')))[1]::text::bigint), 0)
          FROM legacy_tables)::text
  UNION ALL SELECT 6, 'proprietaires des tables', 'information',
         coalesce((SELECT string_agg(DISTINCT relowner::regrole::text, ',') FROM legacy_tables), '')
  UNION ALL SELECT 7, 'vues, vues materialisees, tables etrangeres et types composites dans geonaf', '0',
         (SELECT count(*) FROM legacy WHERE relkind IN ('v', 'm', 'f', 'c'))::text
  UNION ALL SELECT 8, 'sequences de geonaf non rattachees a une colonne identity', '0',
         (SELECT count(*) FROM legacy s WHERE s.relkind = 'S' AND NOT EXISTS (
            SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = s.oid
            AND d.deptype IN ('a', 'i')))::text
  UNION ALL SELECT 9, 'fonctions et procedures dans geonaf', '0',
         (SELECT count(*) FROM pg_proc WHERE pronamespace = to_regnamespace('geonaf'))::text
  UNION ALL SELECT 10, 'domaines, enumerations et intervalles dans geonaf', '0',
         (SELECT count(*) FROM pg_type WHERE typnamespace = to_regnamespace('geonaf')
          AND typtype IN ('d', 'e', 'r', 'm'))::text
  UNION ALL SELECT 11, 'declencheurs sur les tables de geonaf', '0',
         (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN (SELECT oid FROM legacy_tables))::text
  UNION ALL SELECT 12, 'politiques RLS sur les tables de geonaf', 'information',
         (SELECT count(*) FROM pg_policy WHERE polrelid IN (SELECT oid FROM legacy_tables))::text
  UNION ALL SELECT 13, 'vues ou regles hors geonaf qui lisent une table de geonaf', '0',
         (SELECT count(DISTINCT r.ev_class) FROM pg_depend d
          JOIN pg_rewrite r ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
          JOIN pg_class v ON v.oid = r.ev_class
          WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid IN (SELECT oid FROM legacy)
          AND v.relnamespace <> to_regnamespace('geonaf'))::text
  UNION ALL SELECT 14, 'cles etrangeres hors geonaf vers une table de geonaf', '0',
         (SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND confrelid IN (SELECT oid FROM legacy_tables)
          AND connamespace <> to_regnamespace('geonaf'))::text
  UNION ALL SELECT 15, 'fonctions hors geonaf qui citent geonaf dans leur code', '0',
         (SELECT count(*) FROM pg_proc WHERE pronamespace <> to_regnamespace('geonaf')
          AND pronamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
          AND strpos(coalesce(prosrc, ''), 'geonaf') > 0)::text
  UNION ALL SELECT 16, 'parametres de role ou de base qui citent geonaf (search_path, Data API)', '0',
         (SELECT count(*) FROM pg_db_role_setting, unnest(setconfig) AS setting
          WHERE strpos(setting, 'geonaf') > 0)::text
  UNION ALL SELECT 17, 'schema geonaf non expose par la Data API', 'true',
         (NOT coalesce((SELECT bool_or(strpos(setting, 'pgrst.db_schemas=') = 1 AND strpos(setting, 'geonaf') > 0)
                        FROM pg_roles, unnest(rolconfig) AS setting WHERE rolname = 'authenticator'), false))::text
  UNION ALL SELECT 18, 'publications (Realtime) contenant une table de geonaf', '0',
         (SELECT count(*) FROM pg_publication_tables WHERE schemaname = 'geonaf')::text
  UNION ALL SELECT 19, 'taches pg_cron visibles qui citent geonaf (0 si pg_cron absent)', '0',
         CASE WHEN to_regclass('cron.job') IS NULL THEN '0'
              ELSE (xpath('/row/n/text()', query_to_xml(
                     'SELECT count(*) AS n FROM cron.job WHERE strpos(command, ''geonaf'') > 0', false, true, '')))[1]::text
         END
  -- pg_cron filters cron.job by RLS (username = current_user): jobs of other roles are invisible here.
  UNION ALL SELECT 19.5, 'pg_cron : roles proprietaires de taches, visibles seulement pour le role courant', 'information',
         CASE WHEN to_regclass('cron.job') IS NULL THEN 'pg_cron absent'
              ELSE coalesce((xpath('/row/u/text()', query_to_xml(
                     'SELECT string_agg(DISTINCT username, '','') AS u FROM cron.job', false, true, '')))[1]::text,
                   'aucune tache visible') END
  UNION ALL SELECT 20, 'extensions installees dans geonaf', '0',
         (SELECT count(*) FROM pg_extension WHERE extnamespace = to_regnamespace('geonaf'))::text
  UNION ALL SELECT 21, 'droits par defaut definis sur geonaf', 'information',
         (SELECT count(*) FROM pg_default_acl WHERE defaclnamespace = to_regnamespace('geonaf'))::text
  UNION ALL SELECT 22, 'proprietaire du schema geonaf', 'information',
         coalesce((SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'geonaf'), 'absent')
  UNION ALL SELECT 23, 'role courant autorise a renommer (droits du proprietaire herites, CREATE sur la base)', 'true',
         coalesce((SELECT pg_has_role(current_user, nspowner, 'USAGE')
                          AND has_database_privilege(current_database(), 'CREATE')
                   FROM pg_namespace WHERE nspname = 'geonaf'), false)::text
  UNION ALL SELECT 24, 'schema cible geonaf_legacy_draft absent', 'true',
         (to_regnamespace('geonaf_legacy_draft') IS NULL)::text
  UNION ALL SELECT 25, 'autres sessions ouvertes sur la base', 'information',
         (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid())::text
  UNION ALL SELECT 26, 'version et schema de PostGIS', 'information',
         coalesce((SELECT extversion || ' dans ' || extnamespace::regnamespace::text FROM pg_extension
                   WHERE extname = 'postgis'), 'absent')
  -- Event triggers run during ALTER SCHEMA with the rights of their function: each one must be known.
  UNION ALL SELECT 27, 'declencheurs d''evenement actifs (nom:fonction), a relire', 'information',
         coalesce((SELECT string_agg(evtname || ':' || evtfoid::regproc::text, ', ' ORDER BY evtname)
                   FROM pg_event_trigger WHERE evtenabled <> 'D'), 'aucun')
) AS checks
ORDER BY ordre;
