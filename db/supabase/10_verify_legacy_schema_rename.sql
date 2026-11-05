-- GEONAF - Supabase SQL Editor - 10 verification of the legacy schema after its rename (read only: one SELECT).
--
-- For geonaf-test, after 09 application. Paste the whole file in a new query, select nothing, Run, export as CSV.
-- Every line shows attendu, obtenu and statut; the last line gives the verdict GO / NO GO (all blocking lines
-- conforme). GO means: the rename kept the legacy schema intact and nothing refers to it; it does not apply
-- 0001 (next steps: 00, 01, 10_security_precheck, simulation 02, each with its own authorization).
--
-- Expected values of geonaf-test (CTE "attendu", the only lines to change for another project):
-- OID 18655 and the structure fingerprint of export D taken before the rename (scripts/db_backup/legacy_structure.sql).
-- Structure comparison: the rows of legacy_structure.sql are rebuilt here character for character, with only
-- two neutralisations: the qualified prefix geonaf_legacy_draft. is written back as geonaf. (index, constraint,
-- default and policy definitions print it), and the schema comment is replaced by the initial comment kept in
-- the mark. Any other difference (table, column, type, default, constraint, index, sequence, policy, trigger,
-- RLS, table or column comment) changes the fingerprint. The 09 fingerprint changes by design after a rename
-- (it hashes the schema name and comment): it is not compared here.
-- Writes nothing: the first statement makes the whole request a read-only transaction. Row counts read only
-- the tables whose storage is not empty (an empty file proves 0 rows, no query needed).
SELECT set_config('transaction_read_only', 'on', true);
SELECT set_config('search_path', '', true);

WITH attendu AS (
  SELECT 18655::oid AS oid_schema,
         'e00810f8b82477537aad150a90edf19f370a5c8e5d705f1e659079984649505c'::text AS structure_sha256,
         'geonaf-legacy: schema herite trouve le 2026-11-03, renomme avant la migration 0001, non utilise'::text AS prefixe,
         ' ; commentaire initial (JSON) : '::text AS separateur,
         'acquisitions,analysis_acquisitions,analysis_runs,indicator_definitions,indicator_results,sources,territories,territory_versions'::text AS tables
), ns AS (
  SELECT oid, nspowner, nspacl FROM pg_catalog.pg_namespace WHERE nspname = 'geonaf_legacy_draft'
), marque AS (
  SELECT obj_description((SELECT oid FROM ns), 'pg_namespace') AS texte
), initial AS (
  SELECT CASE WHEN left(m.texte, length(a.prefixe || a.separateur)) = a.prefixe || a.separateur
              THEN substr(m.texte, length(a.prefixe || a.separateur) + 1) END AS json
  FROM marque m, attendu a
), rels AS (
  SELECT c.oid, c.relname, c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity, c.relowner, c.relacl
  FROM pg_catalog.pg_class c WHERE c.relnamespace = (SELECT oid FROM ns)
), items_raw AS (
  -- Same rows as scripts/db_backup/legacy_structure.sql; only the schema line takes the initial comment.
  SELECT 'schema'::text AS kind, 'geonaf'::text AS name,
         coalesce((SELECT json FROM initial), 'marque illisible') AS detail
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
), items AS (
  -- The only rewrite: the new schema name printed in definitions goes back to the name of export D.
  SELECT kind, name, regexp_replace(detail, '\mgeonaf_legacy_draft\.', 'geonaf.', 'g') AS detail FROM items_raw
), lignes AS (
  SELECT coalesce(sum(CASE WHEN pg_relation_size(r.oid) = 0 THEN 0
                           ELSE (xpath('/row/n/text()', query_to_xml(
                                  format('SELECT count(*) AS n FROM %I.%I', 'geonaf_legacy_draft', r.relname),
                                  false, true, '')))[1]::text::bigint END), 0) AS total
  FROM rels r WHERE r.relkind IN ('r', 'p')
), api_roles AS (
  SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN ('anon', 'authenticated', 'service_role')
  UNION ALL SELECT 0::oid      -- PUBLIC
), droits_api AS (
  SELECT count(*) AS n FROM (
    SELECT (aclexplode(nspacl)).grantee FROM ns
    UNION ALL
    SELECT (aclexplode(relacl)).grantee FROM rels WHERE relacl IS NOT NULL
  ) AS g WHERE g.grantee IN (SELECT oid FROM api_roles)
), dependances AS (
  -- Objects outside the schema that depend on it by OID (generic check B1, on the renamed schema).
  SELECT count(*) AS n
  FROM pg_catalog.pg_depend d
  WHERE ((d.refclassid = 'pg_catalog.pg_class'::regclass AND d.refobjid IN (SELECT oid FROM rels))
      OR (d.refclassid = 'pg_catalog.pg_type'::regclass AND d.refobjid IN (SELECT t.oid FROM pg_catalog.pg_type t WHERE t.typnamespace = (SELECT oid FROM ns)))
      OR (d.refclassid = 'pg_catalog.pg_namespace'::regclass AND d.refobjid = (SELECT oid FROM ns)))
    AND NOT ((d.classid = 'pg_catalog.pg_class'::regclass AND d.objid IN (SELECT oid FROM rels))
      OR (d.classid = 'pg_catalog.pg_class'::regclass AND d.deptype = 'i' AND d.objid IN (SELECT c.reltoastrelid FROM pg_catalog.pg_class c WHERE c.oid IN (SELECT oid FROM rels)))
      OR (d.classid = 'pg_catalog.pg_type'::regclass AND d.objid IN (SELECT t.oid FROM pg_catalog.pg_type t WHERE t.typnamespace = (SELECT oid FROM ns)))
      OR (d.classid = 'pg_catalog.pg_constraint'::regclass AND d.objid IN (SELECT k.oid FROM pg_catalog.pg_constraint k WHERE k.connamespace = (SELECT oid FROM ns)))
      OR (d.classid = 'pg_catalog.pg_attrdef'::regclass AND d.objid IN (SELECT ad.oid FROM pg_catalog.pg_attrdef ad WHERE ad.adrelid IN (SELECT oid FROM rels)))
      OR (d.classid = 'pg_catalog.pg_policy'::regclass AND d.objid IN (SELECT p.oid FROM pg_catalog.pg_policy p WHERE p.polrelid IN (SELECT oid FROM rels)))
      OR (d.classid = 'pg_catalog.pg_trigger'::regclass AND d.objid IN (SELECT t.oid FROM pg_catalog.pg_trigger t WHERE t.tgrelid IN (SELECT oid FROM rels)))
      OR (d.classid = 'pg_catalog.pg_rewrite'::regclass AND d.objid IN (SELECT w.oid FROM pg_catalog.pg_rewrite w WHERE w.ev_class IN (SELECT oid FROM rels))))
), checks AS (
  SELECT 0 AS ordre, 'transaction en lecture seule' AS controle, 'on' AS attendu,
         current_setting('transaction_read_only') AS obtenu, true AS bloquant
  UNION ALL SELECT 1, 'instance en cours : demarrage UTC (comparer avec 08 l.-3)', 'information',
         to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US'), false
  UNION ALL SELECT 2, 'version PostgreSQL', 'information', current_setting('server_version'), false
  UNION ALL SELECT 3, 'schema geonaf absent (libere pour 0001)', 'absent',
         CASE WHEN to_regnamespace('geonaf') IS NULL THEN 'absent' ELSE 'present' END, true
  UNION ALL SELECT 4, 'schema geonaf_legacy_draft present', 'present',
         CASE WHEN EXISTS (SELECT 1 FROM ns) THEN 'present' ELSE 'absent' END, true
  UNION ALL SELECT 5, 'OID du schema renomme (identique a celui du schema herite)', (SELECT oid_schema::text FROM attendu),
         coalesce((SELECT oid::text FROM ns), 'absent'), true
  UNION ALL SELECT 6, 'marque exacte du schema renomme', (SELECT prefixe || separateur || 'null' FROM attendu),
         coalesce((SELECT texte FROM marque), '(aucune)'), true
  UNION ALL SELECT 7, 'commentaire initial enregistre dans la marque (JSON)', 'null',
         coalesce((SELECT json FROM initial), '(illisible)'), true
  UNION ALL SELECT 8, 'tables du schema renomme', (SELECT tables FROM attendu),
         coalesce((SELECT string_agg(relname, ',' ORDER BY relname COLLATE "C") FROM rels WHERE relkind IN ('r', 'p')), ''), true
  UNION ALL SELECT 9, 'lignes dans les tables du schema renomme', '0', (SELECT total::text FROM lignes), true
  UNION ALL SELECT 10, 'empreinte de structure, nom du schema neutralise (egale a l''export D)',
         (SELECT structure_sha256 FROM attendu),
         encode(sha256(convert_to(coalesce(
           (SELECT string_agg(kind || '|' || name || '|' || detail, E'\n' ORDER BY kind COLLATE "C", name COLLATE "C") FROM items),
           ''), 'UTF8')), 'hex'), true
  UNION ALL SELECT 11, 'elements de structure compares', 'information', (SELECT count(*) FROM items)::text, false
  UNION ALL SELECT 12, 'un seul proprietaire pour le schema et ses tables', '1',
         (SELECT count(DISTINCT o)::text FROM (SELECT nspowner AS o FROM ns UNION ALL SELECT relowner FROM rels) AS owners), true
  UNION ALL SELECT 13, 'proprietaire', 'information', coalesce((SELECT pg_get_userbyid(nspowner) FROM ns), 'absent'), false
  UNION ALL SELECT 14, 'role courant avec les droits du proprietaire et CREATE sur la base (retour arriere possible)', 'true',
         coalesce((SELECT (pg_has_role(current_user, nspowner, 'USAGE')
                           AND has_database_privilege(current_database(), 'CREATE'))::text FROM ns), 'false'), true
  UNION ALL SELECT 15, 'droits accordes a anon, authenticated, service_role ou PUBLIC (schema et tables)', '0',
         (SELECT n::text FROM droits_api), true
  UNION ALL SELECT 16, 'tables avec RLS activee / avec FORCE RLS', 'information',
         (SELECT count(*) FILTER (WHERE relrowsecurity) || ' / ' || count(*) FILTER (WHERE relforcerowsecurity)
          FROM rels WHERE relkind IN ('r', 'p')), false
  UNION ALL SELECT 17, 'politiques RLS', 'information',
         (SELECT count(*)::text FROM pg_catalog.pg_policy WHERE polrelid IN (SELECT oid FROM rels)), false
  UNION ALL SELECT 18, 'droits par defaut definis sur le schema renomme', 'information',
         (SELECT count(*)::text FROM pg_catalog.pg_default_acl WHERE defaclnamespace = (SELECT oid FROM ns)), false
  UNION ALL SELECT 19, 'objets hors du schema qui en dependent (controle B1)', '0', (SELECT n::text FROM dependances), true
  UNION ALL SELECT 20, 'fonctions hors catalogue qui citent geonaf dans leur code ou leur search_path', '0',
         (SELECT count(*)::text FROM pg_catalog.pg_proc WHERE pronamespace NOT IN
            ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
            AND (strpos(coalesce(prosrc, ''), 'geonaf') > 0
                 OR strpos(coalesce(array_to_string(proconfig, ' '), ''), 'geonaf') > 0)), true
  UNION ALL SELECT 21, 'parametres de role ou de base qui citent geonaf', '0',
         (SELECT count(*)::text FROM pg_catalog.pg_db_role_setting, unnest(setconfig) AS setting
          WHERE strpos(setting, 'geonaf') > 0), true
  UNION ALL SELECT 22, 'schemas geonaf* non exposes par la Data API (parametre du role authenticator)', 'true',
         (NOT coalesce((SELECT bool_or(strpos(s.setting, 'pgrst.db_schemas=') = 1 AND strpos(s.setting, 'geonaf') > 0)
                        FROM pg_catalog.pg_roles r, unnest(r.rolconfig) AS s(setting) WHERE r.rolname = 'authenticator'), false))::text, true
  UNION ALL SELECT 23, 'publications contenant une table du schema renomme', '0',
         (SELECT count(*)::text FROM pg_catalog.pg_publication_tables WHERE schemaname IN ('geonaf', 'geonaf_legacy_draft')), true
  UNION ALL SELECT 24, 'declencheurs d''evenement actifs (nom:fonction), a relire', 'information',
         coalesce((SELECT string_agg(evtname || ':' || evtfoid::regproc::text, ', ' ORDER BY evtname)
                   FROM pg_catalog.pg_event_trigger WHERE evtenabled <> 'D'), 'aucun'), false
)
SELECT ordre, controle, attendu, obtenu,
       CASE WHEN NOT bloquant THEN 'information'
            WHEN attendu IS NOT DISTINCT FROM obtenu THEN 'conforme' ELSE 'NON CONFORME' END AS statut
FROM checks
UNION ALL
SELECT 99, 'VERDICT : schema renomme intact, aucune reference ; ne vaut pas application de 0001', 'GO',
       CASE WHEN bool_and(attendu IS NOT DISTINCT FROM obtenu) FILTER (WHERE bloquant) THEN 'GO' ELSE 'NO GO' END,
       (count(*) FILTER (WHERE bloquant AND attendu IS DISTINCT FROM obtenu))::text || ' controle(s) bloquant(s) non conforme(s)'
FROM checks
ORDER BY ordre;
