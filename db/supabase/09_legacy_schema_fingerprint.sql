-- GEONAF - Supabase SQL Editor - 09 fingerprint of the legacy schema, before and after 09 (read only: one SELECT).
--
-- Run just before and just after the 09 simulation (and around 09 application or rollback). For the
-- simulation, every column except execution_utc must be identical in the two exports: same schema
-- name and OID, same exact comment, same tables, columns, constraints, indexes, policies, triggers,
-- and grants (schema, relations, columns, and default privileges of the two schemas). The fingerprint
-- is the SHA-256 of a canonical text of these catalog rows; the search path is empty for the request
-- so that every name in it is printed the same way each time. Row counts are not part of it (they
-- need query_to_xml, absent from servers built without libxml): 08 line 5, run before and after,
-- counts them.
-- Writes nothing: the first statement makes the whole request a read-only transaction.
SELECT set_config('transaction_read_only', 'on', true);
SELECT set_config('search_path', '', true);

WITH schemas AS (
  SELECT oid, nspname, nspowner, nspacl FROM pg_catalog.pg_namespace
  WHERE nspname IN ('geonaf', 'geonaf_legacy_draft')
), rels AS (
  SELECT c.* FROM pg_catalog.pg_class c JOIN schemas s ON c.relnamespace = s.oid
), canonical AS (
  SELECT concat_ws(E'\n',
    (SELECT string_agg(concat_ws('|', 'schema', oid, nspname, nspowner::regrole, nspacl::text,
                                 coalesce(to_jsonb(obj_description(oid, 'pg_namespace'))::text, 'null')),
                       E'\n' ORDER BY nspname) FROM schemas),
    (SELECT string_agg(concat_ws('|', 'relation', r.oid, r.relnamespace, r.relname, r.relkind, r.relowner::regrole,
                                 r.relacl::text, r.relrowsecurity, r.relforcerowsecurity, r.relpersistence,
                                 coalesce(to_jsonb(obj_description(r.oid, 'pg_class'))::text, 'null')),
                       E'\n' ORDER BY r.oid) FROM rels r),
    (SELECT string_agg(concat_ws('|', 'column', a.attrelid, a.attnum, a.attname,
                                 format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attisdropped, a.attidentity,
                                 pg_get_expr(d.adbin, d.adrelid), coalesce(a.attacl::text, '-')),
                       E'\n' ORDER BY a.attrelid, a.attnum)
     FROM pg_catalog.pg_attribute a LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attrelid IN (SELECT oid FROM rels) AND a.attnum > 0),
    (SELECT string_agg(concat_ws('|', 'constraint', k.oid, k.conrelid, k.conname, pg_get_constraintdef(k.oid)),
                       E'\n' ORDER BY k.oid)
     FROM pg_catalog.pg_constraint k WHERE k.connamespace IN (SELECT oid FROM schemas)),
    (SELECT string_agg(concat_ws('|', 'index', i.indexrelid, pg_get_indexdef(i.indexrelid)), E'\n' ORDER BY i.indexrelid)
     FROM pg_catalog.pg_index i WHERE i.indrelid IN (SELECT oid FROM rels)),
    (SELECT string_agg(concat_ws('|', 'policy', p.oid, p.polrelid, p.polname, p.polcmd, p.polroles::text,
                                 pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)),
                       E'\n' ORDER BY p.oid)
     FROM pg_catalog.pg_policy p WHERE p.polrelid IN (SELECT oid FROM rels)),
    (SELECT string_agg(concat_ws('|', 'trigger', g.oid, g.tgrelid, g.tgname, g.tgenabled), E'\n' ORDER BY g.oid)
     FROM pg_catalog.pg_trigger g WHERE g.tgrelid IN (SELECT oid FROM rels) AND NOT g.tgisinternal),
    (SELECT string_agg(concat_ws('|', 'default_acl', da.oid, da.defaclrole::regrole, da.defaclnamespace,
                                 da.defaclobjtype::text, da.defaclacl::text),
                       E'\n' ORDER BY da.oid)
     FROM pg_catalog.pg_default_acl da WHERE da.defaclnamespace IN (SELECT oid FROM schemas))
  ) AS document
)
SELECT
  to_char(transaction_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS execution_utc,
  to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') AS instance_utc,
  current_setting('transaction_read_only') AS lecture_seule,
  coalesce(to_regnamespace('geonaf')::oid::text, 'absent') AS oid_geonaf,
  coalesce(to_regnamespace('geonaf_legacy_draft')::oid::text, 'absent') AS oid_geonaf_legacy_draft,
  coalesce(to_jsonb(obj_description(to_regnamespace('geonaf'), 'pg_namespace'))::text, 'null') AS commentaire_geonaf_json,
  coalesce(to_jsonb(obj_description(to_regnamespace('geonaf_legacy_draft'), 'pg_namespace'))::text, 'null')
    AS commentaire_legacy_draft_json,
  (SELECT count(*) FROM rels WHERE relkind IN ('r', 'p')) AS tables,
  encode(sha256(convert_to(coalesce((SELECT document FROM canonical), ''), 'UTF8')), 'hex') AS empreinte_sha256;
