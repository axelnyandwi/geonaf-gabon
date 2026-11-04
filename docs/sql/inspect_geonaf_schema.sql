-- GEONAF - Supabase SQL Editor - 05 inventory of the deployed geonaf schema (read only: one SELECT).
-- Run on geonaf-dev and on geonaf-test, export each result as CSV (geonaf-dev.csv, geonaf-test.csv).
-- Lists tables (RLS, row counts), columns, constraints, indexes, policies, triggers, views, functions,
-- grants and the environment mark, to be compared with db/sql/0001_territorial_registry.up.sql.
-- Writes nothing: no DDL, no DML, no function with side effects. Row counts use query_to_xml on
-- the geonaf tables only. The output contains structure, never data values or secrets.
WITH t AS (
  SELECT c.oid, c.relname FROM pg_class c
  WHERE c.relnamespace = to_regnamespace('geonaf') AND c.relkind IN ('r', 'p')
)
SELECT objet, table_name, nom, definition FROM (
  SELECT 0 AS ordre, 'environnement' AS objet, '' AS table_name, 'marque' AS nom,
         coalesce(obj_description(to_regnamespace('geonaf'), 'pg_namespace'), '(aucune)') AS definition
  UNION ALL
  SELECT 0, 'extension', '', extname, extversion || ' (schéma ' || extnamespace::regnamespace::text || ')'
  FROM pg_extension WHERE extname = 'postgis'
  UNION ALL
  SELECT 1, 'table', t.relname, t.relname,
         'rls=' || c.relrowsecurity || ' force_rls=' || c.relforcerowsecurity || ' lignes='
         || (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM geonaf.%I', t.relname),
                                                false, true, '')))[1]::text
  FROM t JOIN pg_class c ON c.oid = t.oid
  UNION ALL
  SELECT 2, 'colonne', t.relname, lpad(a.attnum::text, 2, '0') || ' ' || a.attname,
         format_type(a.atttypid, a.atttypmod)
         || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
         || CASE WHEN a.attidentity <> '' THEN ' IDENTITY(' || a.attidentity::text || ')' ELSE '' END
         || coalesce(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '')
  FROM t JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
  UNION ALL
  SELECT 3, 'contrainte', t.relname, k.contype::text || ' ' || k.conname,
         pg_get_constraintdef(k.oid) || CASE WHEN k.condeferrable THEN ' (deferrable)' ELSE '' END
         || CASE WHEN NOT k.convalidated THEN ' (NOT VALID)' ELSE '' END
  FROM t JOIN pg_constraint k ON k.conrelid = t.oid
  UNION ALL
  SELECT 4, 'index', i.tablename, i.indexname, i.indexdef FROM pg_indexes i WHERE i.schemaname = 'geonaf'
  UNION ALL
  SELECT 5, 'politique', p.tablename, p.policyname,
         p.permissive || ' ' || p.cmd || ' TO ' || array_to_string(p.roles, ',')
         || coalesce(' USING (' || p.qual || ')', '') || coalesce(' WITH CHECK (' || p.with_check || ')', '')
  FROM pg_policies p WHERE p.schemaname = 'geonaf'
  UNION ALL
  SELECT 6, 'declencheur', t.relname, g.tgname, pg_get_triggerdef(g.oid)
  FROM t JOIN pg_trigger g ON g.tgrelid = t.oid AND NOT g.tgisinternal
  UNION ALL
  SELECT 7, 'vue', c.relname, c.relname,
         coalesce(array_to_string(c.reloptions, ','), '') || ' ' || pg_get_viewdef(c.oid, true)
  FROM pg_class c WHERE c.relnamespace = to_regnamespace('geonaf') AND c.relkind IN ('v', 'm')
  UNION ALL
  SELECT 8, 'fonction', '', p.oid::regprocedure::text, coalesce(array_to_string(p.proconfig, ','), '')
  FROM pg_proc p WHERE p.pronamespace = to_regnamespace('geonaf')
  UNION ALL
  SELECT 9, 'droit', g.table_name, g.grantee, string_agg(g.privilege_type, ',' ORDER BY g.privilege_type)
  FROM information_schema.role_table_grants g WHERE g.table_schema = 'geonaf'
  GROUP BY g.table_name, g.grantee
) AS inventaire
ORDER BY ordre, table_name, nom;
