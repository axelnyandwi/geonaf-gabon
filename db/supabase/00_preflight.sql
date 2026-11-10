-- GEONAF - Supabase SQL Editor - 00 preflight (read only: SELECT statements only).
-- Run first, on each project (geonaf-dev, then geonaf-test). Every line must read conforme = true,
-- except the informative lines (attendu = 'information').
--
-- The first statement sets a setting local to the transaction; the last one reads it back. If the editor
-- runs the whole text as one request (one implicit transaction), the value is still there: then every
-- GEONAF script is all or nothing. If not, STOP: the other scripts must not be run from this editor.
SELECT set_config('geonaf.preflight_probe', 'meme-transaction', true);

SELECT verification, attendu, obtenu,
       CASE WHEN attendu = 'information' THEN NULL ELSE attendu = obtenu END AS conforme
FROM (
  SELECT 'texte execute en une seule transaction' AS verification, 'meme-transaction' AS attendu,
         coalesce(current_setting('geonaf.preflight_probe', true), '') AS obtenu
  UNION ALL SELECT 'PostgreSQL 15 ou plus', 'true',
         (current_setting('server_version_num')::integer >= 150000)::text
  UNION ALL SELECT 'version PostgreSQL', 'information', current_setting('server_version')
  UNION ALL SELECT 'PostGIS installe', 'true',
         (EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis'))::text
  UNION ALL SELECT 'PostGIS 3.3 ou plus', 'true',
         coalesce((SELECT (split_part(extversion, '.', 1)::integer * 100 + split_part(extversion, '.', 2)::integer
                           >= 303)::text FROM pg_extension WHERE extname = 'postgis'), 'false')
  UNION ALL SELECT 'version et schema de PostGIS', 'information',
         coalesce((SELECT extversion || ' dans ' || extnamespace::regnamespace::text FROM pg_extension
                   WHERE extname = 'postgis'), 'absent')
  UNION ALL SELECT 'role courant', 'postgres', current_user::text
  UNION ALL SELECT 'schema geonaf', 'information',
         CASE WHEN to_regnamespace('geonaf') IS NULL THEN 'absent (migration a appliquer)'
              ELSE 'present : ' || coalesce(obj_description(to_regnamespace('geonaf'), 'pg_namespace'),
                                            'environnement non marque') END
  UNION ALL SELECT 'roles GEONAF existants', 'information',
         coalesce((SELECT string_agg(rolname || CASE WHEN rolcanlogin THEN ' (LOGIN)' ELSE ' (NOLOGIN)' END, ', '
                                     ORDER BY rolname)
                   FROM pg_roles WHERE rolname IN ('geonaf_reader', 'geonaf_importer')), 'aucun')
  UNION ALL SELECT 'schema geonaf non expose par la Data API', 'true',
         (NOT coalesce((SELECT bool_or(setting LIKE 'pgrst.db_schemas=' || '%geonaf%')
                        FROM pg_roles, unnest(rolconfig) AS setting WHERE rolname = 'authenticator'), false))::text
  UNION ALL SELECT 'delai maximal par instruction', 'information', current_setting('statement_timeout')
  UNION ALL SELECT 'base courante', 'information', current_database()::text
) AS checks;
