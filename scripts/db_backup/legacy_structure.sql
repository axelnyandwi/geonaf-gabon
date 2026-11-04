-- GEONAF - structure of the legacy geonaf schema, independent of OIDs and owners (read only: one SELECT).
--
-- Used three ways (docs/supabase_backup_d2_9c.md):
-- 1. Supabase SQL Editor (HTTPS, works from the workstation): paste the whole file, select nothing, Run,
--    export the grid as CSV. This is a structural record, not a backup.
-- 2. Invoke-GeonafSchemaBackup.ps1, through psql in one read-only transaction, next to the pg_dump archive.
-- 3. Restore-GeonafBackupIsolated.ps1, on the restored copy: the line structure_sha256 must be identical.
--
-- section = structure : rows hashed in structure_sha256 (tables, columns, constraints, indexes, sequences,
--                       policies, triggers, comments, RLS). Names only, never OIDs, so a restored copy compares.
-- section = information : owners and grants. Not hashed: a local restore runs with --no-owner --no-acl
--                       because the Supabase roles do not exist locally.
-- Sorting uses COLLATE "C" so that Supabase (en_US.UTF-8) and a local server (C) hash the same text.
-- Writes nothing: the first statement makes the whole request a read-only transaction.
SELECT set_config('transaction_read_only', 'on', true);
SELECT set_config('search_path', '', true);

WITH ns AS (
  SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'geonaf'
), rels AS (
  SELECT c.oid, c.relname, c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity, c.relowner, c.relacl
  FROM pg_catalog.pg_class c WHERE c.relnamespace = (SELECT oid FROM ns)
), items AS (
  SELECT 'schema'::text AS kind, 'geonaf'::text AS name,
         coalesce(to_jsonb(obj_description((SELECT oid FROM ns), 'pg_namespace'))::text, 'null') AS detail
  WHERE EXISTS (SELECT 1 FROM ns)
  UNION ALL
  SELECT 'relation', r.relname,
         concat_ws('|', r.relkind, r.relpersistence, 'rls=' || r.relrowsecurity::text, 'force=' || r.relforcerowsecurity::text,
                   coalesce(to_jsonb(obj_description(r.oid, 'pg_class'))::text, 'null'))
  FROM rels r WHERE r.relkind <> 'i'
  UNION ALL
  SELECT 'column', r.relname || '.' || a.attname,
         concat_ws('|', a.attnum, format_type(a.atttypid, a.atttypmod), 'notnull=' || a.attnotnull::text,
                   'identity=' || a.attidentity::text, 'generated=' || a.attgenerated::text,
                   'default=' || coalesce(pg_get_expr(d.adbin, d.adrelid), ''),
                   coalesce(to_jsonb(col_description(r.oid, a.attnum))::text, 'null'))
  FROM rels r
  JOIN pg_catalog.pg_attribute a ON a.attrelid = r.oid AND a.attnum > 0 AND NOT a.attisdropped
  LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
  WHERE r.relkind IN ('r', 'p', 'v', 'm', 'f')
  UNION ALL
  SELECT 'constraint', r.relname || '.' || k.conname, k.contype::text || '|' || pg_get_constraintdef(k.oid)
  FROM pg_catalog.pg_constraint k JOIN rels r ON r.oid = k.conrelid
  UNION ALL
  SELECT 'index', i.relname, pg_get_indexdef(x.indexrelid)
  FROM pg_catalog.pg_index x JOIN rels i ON i.oid = x.indexrelid
  UNION ALL
  SELECT 'sequence', r.relname,
         concat_ws('|', format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement, s.seqmax, s.seqmin, s.seqcache, s.seqcycle)
  FROM pg_catalog.pg_sequence s JOIN rels r ON r.oid = s.seqrelid
  UNION ALL
  SELECT 'policy', r.relname || '.' || p.polname,
         concat_ws('|', p.polcmd, p.polpermissive,
                   (SELECT string_agg(CASE WHEN rid = 0 THEN 'public' ELSE rid::regrole::text END, ',' ORDER BY 1)
                    FROM unnest(p.polroles) AS rid),
                   pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid))
  FROM pg_catalog.pg_policy p JOIN rels r ON r.oid = p.polrelid
  UNION ALL
  SELECT 'trigger', r.relname || '.' || t.tgname, pg_get_triggerdef(t.oid)
  FROM pg_catalog.pg_trigger t JOIN rels r ON r.oid = t.tgrelid WHERE NOT t.tgisinternal
), info AS (
  SELECT 'schema owner'::text AS kind, n.nspname::text AS name,
         concat_ws('|', pg_get_userbyid(n.nspowner), coalesce(n.nspacl::text, 'acl par defaut')) AS detail
  FROM pg_catalog.pg_namespace n WHERE n.oid = (SELECT oid FROM ns)
  UNION ALL
  SELECT 'owner and grants', r.relname, concat_ws('|', pg_get_userbyid(r.relowner), coalesce(r.relacl::text, 'acl par defaut'))
  FROM rels r WHERE r.relkind <> 'i'
  UNION ALL
  SELECT 'default privileges', coalesce(pg_get_userbyid(da.defaclrole), ''), da.defaclobjtype::text || '|' || da.defaclacl::text
  FROM pg_catalog.pg_default_acl da WHERE da.defaclnamespace = (SELECT oid FROM ns)
)
SELECT section, kind, name, detail FROM (
  SELECT 'structure' AS section, kind, name, detail FROM items
  UNION ALL
  SELECT 'information', kind, name, detail FROM info
  UNION ALL
  SELECT 'empreinte', 'structure_sha256', (SELECT count(*) FROM items)::text,
         encode(sha256(convert_to(coalesce(
           (SELECT string_agg(kind || '|' || name || '|' || detail, E'\n' ORDER BY kind COLLATE "C", name COLLATE "C") FROM items),
           ''), 'UTF8')), 'hex')
) AS result
ORDER BY section COLLATE "C", kind COLLATE "C", name COLLATE "C";
