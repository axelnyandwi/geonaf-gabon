-- GEONAF - Supabase SQL Editor - 10 security checks before migration 0001 (read only: one SELECT).
--
-- Run on each project after 09 (rename) and 01 (roles), before 02. Every line must read conforme = true;
-- the informative lines (attendu = 'information') are read by a person against
-- docs/supabase_security_d2_2.md, section "Lecture des lignes d'information". A false line: do not apply 0001.
-- Writes nothing: the first statement makes the whole request a read-only transaction.
-- Output: structure, role names and settings only; never a password, a key or a data value.
SELECT set_config('transaction_read_only', 'on', true);

WITH api_roles AS (
  SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated', 'service_role')
  UNION ALL SELECT 'public'
), geonaf_roles AS (
  SELECT * FROM pg_roles WHERE rolname IN ('geonaf_reader', 'geonaf_importer')
), postgis_names AS (
  SELECT unnest(ARRAY['geometry', 'st_isvalid', 'st_isempty', 'st_numgeometries', 'st_npoints', 'st_xmin', 'st_xmax',
                      'st_ymin', 'st_ymax', 'st_asbinary', 'st_intersects', 'st_geomfromwkb', 'st_multi',
                      'st_setsrid', 'st_makeenvelope']) AS name
)
SELECT verification, attendu, obtenu,
       CASE WHEN attendu = 'information' THEN NULL ELSE attendu = obtenu END AS conforme
FROM (
  SELECT -3 AS ordre, 'transaction en lecture seule' AS verification, 'on' AS attendu,
         current_setting('transaction_read_only') AS obtenu
  UNION ALL SELECT -2, 'empreinte du serveur (meme valeur que la ligne 0b de 08 sur ce projet)', 'information',
         CASE WHEN has_function_privilege('pg_control_system()', 'EXECUTE')
              THEN left(md5((xpath('/row/s/text()', query_to_xml(
                     'SELECT system_identifier AS s FROM pg_control_system()', false, true, '')))[1]::text), 12)
              ELSE 'non disponible (droit EXECUTE absent)' END
  UNION ALL SELECT -1, 'demarrage du serveur et version', 'information',
         pg_postmaster_start_time()::text || ' ; ' || current_setting('server_version')

  -- Target state before 0001.
  UNION ALL SELECT 1, 'role courant', 'postgres', current_user::text
  UNION ALL SELECT 2, 'schema geonaf absent (renomme par 09, migration a appliquer)', 'true',
         (to_regnamespace('geonaf') IS NULL)::text
  UNION ALL SELECT 3, 'schema herite renomme et marque geonaf-legacy', 'true',
         coalesce(strpos(obj_description(to_regnamespace('geonaf_legacy_draft'), 'pg_namespace'), 'geonaf-legacy:') = 1,
                  false)::text
  UNION ALL SELECT 4, 'droits des roles API et de PUBLIC sur geonaf_legacy_draft', '0',
         (SELECT count(*) FROM information_schema.role_table_grants
          WHERE table_schema = 'geonaf_legacy_draft' AND grantee IN ('anon', 'authenticated', 'service_role', 'PUBLIC'))::text
  UNION ALL SELECT 5, 'usage de geonaf_legacy_draft par les roles API ou PUBLIC', 'false',
         coalesce((SELECT bool_or(has_schema_privilege(rolname, 'geonaf_legacy_draft', 'USAGE')) FROM api_roles
                   WHERE to_regnamespace('geonaf_legacy_draft') IS NOT NULL), false)::text

  -- search_path: the migration resolves PostGIS through 'pg_catalog, public, extensions'. An object of
  -- the same name in public (searched before extensions) or in pg_temp would be bound for good to the
  -- constraints and indexes of 0001.
  UNION ALL SELECT 10, 'schema de PostGIS', 'extensions',
         coalesce((SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'postgis'), 'absent')
  UNION ALL SELECT 11, 'objets homonymes de PostGIS hors du schema de PostGIS (masquage par search_path)', '0',
         ((SELECT count(*) FROM pg_type t JOIN postgis_names n ON t.typname = n.name
           WHERE t.typnamespace <> (SELECT extnamespace FROM pg_extension WHERE extname = 'postgis'))
          + (SELECT count(*) FROM pg_proc p JOIN postgis_names n ON p.proname = n.name
             WHERE p.pronamespace <> (SELECT extnamespace FROM pg_extension WHERE extname = 'postgis')))::text
  UNION ALL SELECT 12, 'objets temporaires dans la session de l''editeur', '0',
         (SELECT count(*) FROM pg_class WHERE pg_my_temp_schema() <> 0 AND relnamespace = pg_my_temp_schema())::text
  UNION ALL SELECT 13, 'roles API ou PUBLIC autorises a creer dans public', 'information',
         coalesce((SELECT string_agg(rolname, ',' ORDER BY rolname) FROM api_roles
                   WHERE has_schema_privilege(rolname, 'public', 'CREATE')), 'aucun')
  UNION ALL SELECT 14, 'roles API ou PUBLIC autorises a creer dans extensions', 'aucun',
         coalesce((SELECT string_agg(rolname, ',' ORDER BY rolname) FROM api_roles
                   WHERE to_regnamespace('extensions') IS NOT NULL
                   AND has_schema_privilege(rolname, 'extensions', 'CREATE')), 'aucun')
  UNION ALL SELECT 15, 'search_path declares par role ou base (role:valeur)', 'information',
         coalesce((SELECT string_agg(coalesce(r.rolname, '*') || ':' || s.setting, ' ; ' ORDER BY r.rolname)
                   FROM pg_db_role_setting d LEFT JOIN pg_roles r ON r.oid = d.setrole,
                        unnest(d.setconfig) AS s(setting) WHERE strpos(s.setting, 'search_path=') = 1), 'aucun')

  -- GEONAF roles (01_roles.sql is idempotent: a role that already existed is kept as it is).
  UNION ALL SELECT 20, 'roles GEONAF presents', 'geonaf_importer,geonaf_reader',
         coalesce((SELECT string_agg(rolname, ',' ORDER BY rolname) FROM geonaf_roles), '')
  UNION ALL SELECT 21, 'roles GEONAF sans LOGIN, SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB ni REPLICATION', 'true',
         coalesce((SELECT bool_and(NOT (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb
                                        OR rolreplication)) FROM geonaf_roles), false)::text
  UNION ALL SELECT 22, 'roles GEONAF membres d''un autre role (droits herites)', '0',
         (SELECT count(*) FROM pg_auth_members WHERE member IN (SELECT oid FROM geonaf_roles))::text
  UNION ALL SELECT 23, 'roles GEONAF avec des parametres propres', '0',
         (SELECT count(*) FROM geonaf_roles WHERE rolconfig IS NOT NULL)::text
  UNION ALL SELECT 24, 'roles GEONAF avec un mot de passe ou une date d''expiration', 'information',
         'non lisible sans droit superutilisateur : NOLOGIN (ligne 21) empeche toute connexion'
  UNION ALL SELECT 25, 'membres des roles GEONAF (membre:option admin)', 'information',
         coalesce((SELECT string_agg(m.member::regrole::text || ':' || m.admin_option, ', ' ORDER BY m.member::regrole::text)
                   FROM pg_auth_members m WHERE m.roleid IN (SELECT oid FROM geonaf_roles)), 'aucun')

  -- Privileges that bypass the GEONAF grants or the row level security.
  UNION ALL SELECT 30, 'roles SUPERUSER ou BYPASSRLS (a relire)', 'information',
         coalesce((SELECT string_agg(rolname || CASE WHEN rolsuper THEN '(super)' ELSE '' END
                                     || CASE WHEN rolbypassrls THEN '(bypassrls)' ELSE '' END
                                     || CASE WHEN rolcanlogin THEN '(login)' ELSE '' END, ', ' ORDER BY rolname)
                   FROM pg_roles WHERE rolsuper OR rolbypassrls), 'aucun')
  UNION ALL SELECT 31, 'membres de pg_read_all_data, pg_write_all_data et pg_maintain (lecture ou ecriture sans droit de table)', 'information',
         coalesce((SELECT string_agg(m.roleid::regrole::text || '>' || m.member::regrole::text, ', '
                                     ORDER BY m.roleid::regrole::text, m.member::regrole::text)
                   FROM pg_auth_members m
                   WHERE m.roleid::regrole::text IN ('pg_read_all_data', 'pg_write_all_data', 'pg_maintain')), 'aucun')
  UNION ALL SELECT 32, 'droits par defaut globaux pour les roles API ou PUBLIC (proprietaire:type:beneficiaire)', 'information',
         coalesce((SELECT string_agg(DISTINCT d.defaclrole::regrole::text || ':' || d.defaclobjtype || ':'
                                     || CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, ', ')
                   FROM pg_default_acl d, aclexplode(d.defaclacl) a
                   WHERE d.defaclnamespace = 0
                   AND (a.grantee = 0 OR a.grantee::regrole::text IN ('anon', 'authenticated', 'service_role'))), 'aucun')
  UNION ALL SELECT 34, 'declencheurs d''evenement actifs (nom:fonction:proprietaire), a relire', 'information',
         coalesce((SELECT string_agg(evtname || ':' || evtfoid::regproc::text || ':' || evtowner::regrole::text, ', '
                                     ORDER BY evtname) FROM pg_event_trigger WHERE evtenabled <> 'D'), 'aucun')

  -- Supabase Data API (PostgREST) and GraphQL. Only pgrst.db_schemas is printed: other pgrst.* role
  -- settings may hold a secret (pgrst.jwt_secret) and are never read here.
  UNION ALL SELECT 40, 'schemas exposes declares dans les parametres de role (pgrst.db_schemas)', 'information',
         coalesce((SELECT string_agg(r.rolname || ':' || s.setting, ' ; ')
                   FROM pg_roles r, unnest(r.rolconfig) AS s(setting) WHERE strpos(s.setting, 'pgrst.db_schemas=') = 1),
                  'aucun (verifier aussi le tableau de bord)')
  UNION ALL SELECT 41, 'geonaf ou geonaf_legacy_draft dans pgrst.db_schemas', 'false',
         coalesce((SELECT bool_or(strpos(s.setting, 'pgrst.db_schemas=') = 1 AND strpos(s.setting, 'geonaf') > 0)
                   FROM pg_roles r, unnest(r.rolconfig) AS s(setting)), false)::text
  UNION ALL SELECT 42, 'pg_graphql installe', 'information',
         coalesce((SELECT extversion || ' dans ' || extnamespace::regnamespace::text FROM pg_extension
                   WHERE extname = 'pg_graphql'), 'absent')
) AS checks
ORDER BY ordre;
