-- GEONAF - Supabase SQL Editor - 04 schema check after migration 0001 (read only: one SELECT).
-- Every line must read conforme = true.
WITH geonaf_tables AS (
  SELECT c.oid, c.relname, c.relrowsecurity FROM pg_class c
  WHERE c.relnamespace = to_regnamespace('geonaf') AND c.relkind = 'r'
)
SELECT verification, attendu, obtenu, attendu = obtenu AS conforme FROM (
  SELECT 'migration enregistree' AS verification, '0001_territorial_registry' AS attendu,
         coalesce((SELECT string_agg(version, ',' ORDER BY version) FROM geonaf.schema_migrations), '') AS obtenu
  UNION ALL SELECT 'tables du schema geonaf', '10', (SELECT count(*)::text FROM geonaf_tables)
  UNION ALL SELECT 'tables sans RLS', '0', (SELECT count(*)::text FROM geonaf_tables WHERE NOT relrowsecurity)
  UNION ALL SELECT 'tables sans declencheur d''immuabilite (UPDATE, DELETE et TRUNCATE)', '0',
         (SELECT count(*)::text FROM geonaf_tables t WHERE (SELECT count(*) FROM pg_trigger g
           WHERE g.tgrelid = t.oid AND NOT g.tgisinternal AND g.tgfoid IN (
             'geonaf.forbid_change()'::regprocedure, 'geonaf.close_import_only()'::regprocedure)) < 2)
  UNION ALL SELECT 'droits des roles API Supabase et de PUBLIC sur geonaf', '0',
         (SELECT count(*)::text FROM information_schema.role_table_grants
          WHERE table_schema = 'geonaf' AND grantee IN ('anon', 'authenticated', 'service_role', 'PUBLIC'))
  UNION ALL SELECT 'usage du schema geonaf par les roles API', 'false',
         (SELECT coalesce(bool_or(has_schema_privilege(rolname, 'geonaf', 'USAGE')), false)::text
          FROM pg_roles WHERE rolname IN ('anon', 'authenticated', 'service_role'))
  UNION ALL SELECT 'fonctions sans search_path fixe', '0',
         (SELECT count(*)::text FROM pg_proc WHERE pronamespace = to_regnamespace('geonaf')
          AND NOT coalesce(proconfig::text LIKE '%search_path=%', false))
  UNION ALL SELECT 'vues sans security_invoker', '0',
         (SELECT count(*)::text FROM pg_class WHERE relnamespace = to_regnamespace('geonaf') AND relkind = 'v'
          AND NOT coalesce('security_invoker=true' = ANY (reloptions), false))
  UNION ALL SELECT 'index spatial GiST des geometries', 'true',
         (EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'geonaf' AND indexname = 'geometries_geom_gist'
                  AND indexdef LIKE '%USING gist%'))::text
  UNION ALL SELECT 'licences initiales', 'CC-BY-3.0,CC-BY-3.0-IGO,LicenseRef-GEONAF',
         (SELECT string_agg(licence_id, ',' ORDER BY licence_id) FROM geonaf.licences)
  UNION ALL SELECT 'statuts juridiques', '7', (SELECT count(*)::text FROM geonaf.legal_statuses)
  UNION ALL SELECT 'types de territoires', '4', (SELECT count(*)::text FROM geonaf.territory_kinds)
  UNION ALL SELECT 'lecture de geonaf par geonaf_reader', 'true',
         coalesce((SELECT has_table_privilege('geonaf_reader', 'geonaf.territory_versions', 'SELECT')::text
                   FROM pg_roles WHERE rolname = 'geonaf_reader'), 'role absent')
  UNION ALL SELECT 'ecriture interdite a geonaf_reader', 'false',
         coalesce((SELECT has_table_privilege('geonaf_reader', 'geonaf.territory_versions', 'INSERT')::text
                   FROM pg_roles WHERE rolname = 'geonaf_reader'), 'role absent')
  UNION ALL SELECT 'insertion des versions par geonaf_importer', 'true',
         coalesce((SELECT has_table_privilege('geonaf_importer', 'geonaf.territory_versions', 'INSERT')::text
                   FROM pg_roles WHERE rolname = 'geonaf_importer'), 'role absent')
  UNION ALL SELECT 'modification et licences interdites a geonaf_importer', 'false,false',
         coalesce((SELECT has_table_privilege('geonaf_importer', 'geonaf.territory_versions', 'UPDATE')::text || ','
                          || has_table_privilege('geonaf_importer', 'geonaf.licences', 'INSERT')::text
                   FROM pg_roles WHERE rolname = 'geonaf_importer'), 'role absent')
  UNION ALL SELECT 'schema geonaf non expose par la Data API', 'true',
         (NOT coalesce((SELECT bool_or(setting LIKE 'pgrst.db_schemas=' || '%geonaf%')
                        FROM pg_roles, unnest(rolconfig) AS setting WHERE rolname = 'authenticator'), false))::text
) AS checks;
