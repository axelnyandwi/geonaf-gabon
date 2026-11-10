-- GEONAF - Supabase SQL Editor - 13 verify the Edge reader role (D5). READ ONLY, no validation needed.
--
-- Run after 13_edge_reader_role.sql (application), then again after the LOGIN/credential step.
-- Effective privileges are read with has_*_privilege (PUBLIC and inherited grants included), never assumed.
-- Line 2 is expected false before the credential step and true after it. No setting value is printed:
-- role settings are only compared with the expected text.
SELECT set_config('transaction_read_only', 'on', true);

WITH edge AS (
  SELECT oid, rolname, rolcanlogin, rolsuper, rolbypassrls, rolinherit, rolcreaterole, rolcreatedb, rolreplication,
         rolconnlimit, coalesce(rolconfig, '{}') AS config
  FROM pg_catalog.pg_roles WHERE rolname = 'geonaf_edge_reader'
), granted(table_name, column_name) AS (VALUES
  ('acquisitions', 'collection'), ('acquisitions', 'product_id'), ('acquisitions', 'acquired_at'),
  ('acquisition_manifests', 'manifest_sha256'), ('acquisition_manifests', 'collection'),
  ('acquisition_manifests', 'product_id'), ('acquisition_manifests', 'ingested_at'),
  ('acquisition_manifests', 'grid_crs'), ('acquisition_manifests', 'grid_resolution_m'),
  ('acquisition_manifests', 'integrity_mode')
), geonaf_relations AS (
  SELECT c.oid, c.relname FROM pg_catalog.pg_class c
  WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf') AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
), checks(ordre, controle, attendu, obtenu) AS (
  SELECT 1, 'role geonaf_edge_reader present', 'true', (EXISTS (SELECT 1 FROM edge))::text
  UNION ALL SELECT 2, 'LOGIN (faux avant l''etape identifiant, vrai apres)', 'information',
         coalesce((SELECT rolcanlogin::text FROM edge), 'role absent')
  UNION ALL SELECT 3, 'ni SUPERUSER, BYPASSRLS, INHERIT, CREATEROLE, CREATEDB ni REPLICATION', 'true',
         coalesce((SELECT (NOT (rolsuper OR rolbypassrls OR rolinherit OR rolcreaterole OR rolcreatedb
                                OR rolreplication))::text FROM edge), 'role absent')
  UNION ALL SELECT 4, 'membre d''un autre role (droits herites)', '0',
         (SELECT count(*)::text FROM pg_catalog.pg_auth_members m JOIN edge e ON e.oid = m.member)
  UNION ALL SELECT 5, 'limite de connexions', '5', coalesce((SELECT rolconnlimit::text FROM edge), 'role absent')
  UNION ALL SELECT 6, 'parametres de role statement_timeout, idle_in_transaction, lecture seule', 'true',
         coalesce((SELECT ('statement_timeout=5s' = ANY (config)
                           AND 'idle_in_transaction_session_timeout=10s' = ANY (config)
                           AND 'default_transaction_read_only=on' = ANY (config))::text FROM edge), 'role absent')
  UNION ALL SELECT 7, 'environnement du projet', 'geonaf-environment:test',
         coalesce(pg_catalog.obj_description(pg_catalog.to_regnamespace('geonaf'), 'pg_namespace'), 'absent')
  UNION ALL SELECT 10, 'USAGE sur le schema geonaf', 'true',
         CASE WHEN EXISTS (SELECT 1 FROM edge) AND pg_catalog.to_regnamespace('geonaf') IS NOT NULL
              THEN has_schema_privilege('geonaf_edge_reader', 'geonaf', 'USAGE')::text ELSE 'absent' END
  UNION ALL SELECT 11, 'CREATE sur le schema geonaf', 'false',
         CASE WHEN EXISTS (SELECT 1 FROM edge) AND pg_catalog.to_regnamespace('geonaf') IS NOT NULL
              THEN has_schema_privilege('geonaf_edge_reader', 'geonaf', 'CREATE')::text ELSE 'absent' END
  UNION ALL SELECT 12, 'colonnes attendues lisibles (10)', '10',
         CASE WHEN EXISTS (SELECT 1 FROM edge) THEN
           (SELECT count(*)::text FROM granted g JOIN geonaf_relations r ON r.relname = g.table_name
            WHERE has_column_privilege('geonaf_edge_reader', r.oid, g.column_name, 'SELECT')) ELSE 'absent' END
  UNION ALL SELECT 13, 'autres colonnes lisibles dans les deux tables (manifest_document, stac_source, footprint...)', '0',
         CASE WHEN EXISTS (SELECT 1 FROM edge) THEN
           (SELECT count(*)::text FROM geonaf_relations r JOIN pg_catalog.pg_attribute a ON a.attrelid = r.oid
            WHERE r.relname IN ('acquisitions', 'acquisition_manifests') AND a.attnum > 0 AND NOT a.attisdropped
              AND NOT EXISTS (SELECT 1 FROM granted g WHERE g.table_name = r.relname AND g.column_name = a.attname)
              AND has_column_privilege('geonaf_edge_reader', r.oid, a.attnum, 'SELECT')) ELSE 'absent' END
  UNION ALL SELECT 14, 'relations geonaf avec un droit quelconque hors colonnes attendues', 'aucune',
         CASE WHEN EXISTS (SELECT 1 FROM edge) THEN
           coalesce((SELECT string_agg(r.relname, ',' ORDER BY r.relname) FROM geonaf_relations r
                     WHERE has_table_privilege('geonaf_edge_reader', r.oid,
                                               'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                        OR has_any_column_privilege('geonaf_edge_reader', r.oid, 'INSERT,UPDATE,REFERENCES')
                        OR (r.relname NOT IN ('acquisitions', 'acquisition_manifests')
                            AND has_any_column_privilege('geonaf_edge_reader', r.oid, 'SELECT'))), 'aucune')
         ELSE 'absent' END
  UNION ALL SELECT 15, 'fonctions geonaf executables', '0',
         CASE WHEN EXISTS (SELECT 1 FROM edge) THEN
           (SELECT count(*)::text FROM pg_catalog.pg_proc p
            WHERE p.pronamespace = pg_catalog.to_regnamespace('geonaf')
              AND has_function_privilege('geonaf_edge_reader', p.oid, 'EXECUTE')) ELSE 'absent' END
  UNION ALL SELECT 20, 'RLS active sur acquisitions et acquisition_manifests', 'true',
         coalesce((SELECT bool_and(c.relrowsecurity)::text FROM pg_catalog.pg_class c JOIN geonaf_relations r
                   ON r.oid = c.oid WHERE r.relname IN ('acquisitions', 'acquisition_manifests')), 'absent')
  UNION ALL SELECT 21, 'politiques du role Edge (table:commande)', 'acquisition_manifests:SELECT,acquisitions:SELECT',
         coalesce((SELECT string_agg(tablename || ':' || cmd, ',' ORDER BY tablename) FROM pg_catalog.pg_policies
                   WHERE schemaname = 'geonaf' AND 'geonaf_edge_reader' = ANY (roles)), 'aucune')
  UNION ALL SELECT 22, 'politiques geonaf TO PUBLIC (s''appliqueraient au role Edge)', '0',
         (SELECT count(*)::text FROM pg_catalog.pg_policies WHERE schemaname = 'geonaf' AND 'public' = ANY (roles))
  -- Privileges every role gets through PUBLIC: PostgreSQL has no per-role deny, so they cannot be removed for
  -- this role alone without changing them for all roles. Shown so that a reviewer sees the real surface.
  UNION ALL SELECT 30, 'schemas ou le role peut creer des objets', 'aucun',
         CASE WHEN EXISTS (SELECT 1 FROM edge) THEN
           coalesce((SELECT string_agg(n.nspname, ',' ORDER BY n.nspname) FROM pg_catalog.pg_namespace n
                     WHERE has_schema_privilege('geonaf_edge_reader', n.oid, 'CREATE')), 'aucun')
         ELSE 'absent' END
  UNION ALL SELECT 31, 'TEMP sur la base (herite de PUBLIC ; bloque par la lecture seule par defaut)', 'information',
         CASE WHEN EXISTS (SELECT 1 FROM edge)
              THEN has_database_privilege('geonaf_edge_reader', pg_catalog.current_database(), 'TEMP')::text
              ELSE 'absent' END
  UNION ALL SELECT 32, 'schemas utilisables hors geonaf et catalogues (herites de PUBLIC)', 'information',
         CASE WHEN EXISTS (SELECT 1 FROM edge) THEN
           coalesce((SELECT string_agg(n.nspname, ',' ORDER BY n.nspname) FROM pg_catalog.pg_namespace n
                     WHERE n.nspname NOT IN ('geonaf', 'pg_catalog', 'information_schema')
                       AND n.nspname NOT LIKE 'pg\_temp\_%' AND n.nspname NOT LIKE 'pg\_toast%'
                       AND has_schema_privilege('geonaf_edge_reader', n.oid, 'USAGE')), 'aucun')
         ELSE 'absent' END
  UNION ALL SELECT 33, 'fonctions SECURITY DEFINER executables hors pg_catalog (schema:nombre)', 'information',
         CASE WHEN EXISTS (SELECT 1 FROM edge) THEN
           coalesce((SELECT string_agg(s.nspname || ':' || s.total, ',' ORDER BY s.nspname) FROM (
                       SELECT n.nspname, count(*)::text AS total FROM pg_catalog.pg_proc p
                       JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                       WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                         AND has_schema_privilege('geonaf_edge_reader', n.oid, 'USAGE')
                         AND has_function_privilege('geonaf_edge_reader', p.oid, 'EXECUTE')
                       GROUP BY n.nspname) AS s), 'aucune')
         ELSE 'absent' END
  UNION ALL SELECT 34, 'expiration du mot de passe (VALID UNTIL)', 'information',
         coalesce((SELECT coalesce(rolvaliduntil::text, 'aucune') FROM pg_catalog.pg_roles
                   WHERE rolname = 'geonaf_edge_reader'), 'role absent')
)
SELECT ordre, controle, attendu, obtenu,
       CASE WHEN attendu = 'information' THEN NULL ELSE attendu = obtenu END AS conforme
FROM checks ORDER BY ordre;
