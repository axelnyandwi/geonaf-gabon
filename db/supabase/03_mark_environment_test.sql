-- GEONAF - 03 mark ONLY the independently identified test project, immediately after 0001.
-- I2: verify name/reference in the dashboard NOW and record them privately with UTC time.
-- Then copy the start time from a fresh 11 diagnostic; it proves continuity, NOT project identity.
-- Never enter a real project reference here. Send the whole file, without concurrent administration.
-- All guards and the sole persistent write belong to ONE atomic statement.
DO $mark$
DECLARE
  confirmation_i2 constant boolean := false; -- True ONLY after the fresh private I2 record.
  confirmation_instance constant text := 'A_COMPLETER_DEPUIS_11';
  current_mark text;
  history_ok boolean;
BEGIN
  PERFORM pg_catalog.set_config('search_path', '', true);
  IF pg_catalog.to_regnamespace('geonaf') IS NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf absent, appliquer la migration avant de marquer le projet';
  END IF;
  current_mark := pg_catalog.obj_description(pg_catalog.to_regnamespace('geonaf'), 'pg_namespace');
  IF current_mark IS NOT NULL AND current_mark <> 'geonaf-environment:test' THEN
    RAISE EXCEPTION 'GEONAF: projet deja marque comme autre environnement';
  END IF;
  -- Exact inventory: names shared with the historical schema do not identify 0001.
  IF (SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_catalog.pg_class
      WHERE relnamespace = pg_catalog.to_regnamespace('geonaf') AND relkind IN ('r', 'p', 'f'))
     IS DISTINCT FROM 'data_sources,geometries,legal_statuses,licences,registry_imports,schema_migrations,source_releases,territories,territory_kinds,territory_versions' THEN
    RAISE EXCEPTION 'GEONAF: structure post-0001 attendue, schema historique ou incomplet refuse';
  END IF;
  LOCK TABLE geonaf.data_sources, geonaf.geometries, geonaf.legal_statuses, geonaf.licences,
    geonaf.registry_imports, geonaf.schema_migrations, geonaf.source_releases, geonaf.territories,
    geonaf.territory_kinds, geonaf.territory_versions IN ACCESS EXCLUSIVE MODE;
  IF EXISTS (
    WITH expected(table_name, column_name, type_name, not_null, identity_kind) AS (VALUES
      ('licences', 'licence_id', 'text', true, ''),
      ('licences', 'name', 'text', true, ''),
      ('licences', 'url', 'text', false, ''),
      ('licences', 'share_alike', 'boolean', true, ''),
      ('licences', 'commercial_use', 'boolean', false, ''),
      ('licences', 'redistribution', 'boolean', false, ''),
      ('licences', 'reviewed_note', 'text', false, ''),
      ('data_sources', 'source_id', 'text', true, ''),
      ('data_sources', 'provider', 'text', false, ''),
      ('data_sources', 'nature', 'text', false, ''),
      ('data_sources', 'access_tier', 'text', true, ''),
      ('source_releases', 'release_id', 'bigint', true, 'a'),
      ('source_releases', 'source_id', 'text', true, ''),
      ('source_releases', 'product', 'text', true, ''),
      ('source_releases', 'version', 'text', true, ''),
      ('source_releases', 'licence_id', 'text', true, ''),
      ('source_releases', 'attribution', 'text', true, ''),
      ('source_releases', 'url', 'text', false, ''),
      ('source_releases', 'share_alike', 'boolean', true, ''),
      ('source_releases', 'reference_year', 'integer', false, ''),
      ('geometries', 'geometry_sha256', 'character(64)', true, ''),
      ('geometries', 'identity_version', 'text', true, ''),
      ('geometries', 'source_geometry_type', 'text', true, ''),
      ('geometries', 'geom', 'extensions.geometry(MultiPolygon,4326)', true, ''),
      ('geometries', 'parts', 'integer', true, ''),
      ('geometries', 'vertices', 'integer', true, ''),
      ('territory_kinds', 'kind', 'text', true, ''),
      ('legal_statuses', 'kind', 'text', true, ''),
      ('legal_statuses', 'legal_status', 'text', true, ''),
      ('territories', 'territory_id', 'text', true, ''),
      ('territories', 'kind', 'text', true, ''),
      ('registry_imports', 'import_id', 'bigint', true, 'a'),
      ('registry_imports', 'started_at', 'timestamp with time zone', true, ''),
      ('registry_imports', 'finished_at', 'timestamp with time zone', false, ''),
      ('registry_imports', 'source_label', 'text', true, ''),
      ('registry_imports', 'code_version', 'text', true, ''),
      ('registry_imports', 'records_seen', 'integer', true, ''),
      ('registry_imports', 'records_inserted', 'integer', true, ''),
      ('registry_imports', 'records_existing', 'integer', true, ''),
      ('registry_imports', 'fingerprints_sha256', 'character(64)', false, ''),
      ('territory_versions', 'territory_id', 'text', true, ''),
      ('territory_versions', 'version', 'integer', true, ''),
      ('territory_versions', 'kind', 'text', true, ''),
      ('territory_versions', 'label', 'text', true, ''),
      ('territory_versions', 'legal_status', 'text', true, ''),
      ('territory_versions', 'legal_evidence', 'text', false, ''),
      ('territory_versions', 'release_id', 'bigint', true, ''),
      ('territory_versions', 'retrieved_at_utc', 'text', false, ''),
      ('territory_versions', 'geometry_sha256', 'character(64)', true, ''),
      ('territory_versions', 'file_sha256', 'character(64)', true, ''),
      ('territory_versions', 'storage_key', 'text', true, ''),
      ('territory_versions', 'wgs84_bounds', 'double precision[]', true, ''),
      ('territory_versions', 'area_km2', 'double precision', true, ''),
      ('territory_versions', 'vertices', 'integer', true, ''),
      ('territory_versions', 'parts', 'integer', true, ''),
      ('territory_versions', 'created_at_utc', 'text', true, ''),
      ('territory_versions', 'country_iso3', 'character(3)', false, ''),
      ('territory_versions', 'admin_level_code', 'text', false, ''),
      ('territory_versions', 'local_type', 'text', false, ''),
      ('territory_versions', 'source_unit_id', 'text', false, ''),
      ('territory_versions', 'parent_territory_id', 'text', false, ''),
      ('territory_versions', 'grid_system', 'text', false, ''),
      ('territory_versions', 'grid_cell_id', 'text', false, ''),
      ('territory_versions', 'grid_cell_size_deg', 'double precision', false, ''),
      ('territory_versions', 'scientific_profile_id', 'text', false, ''),
      ('territory_versions', 'limits', 'text[]', true, ''),
      ('territory_versions', 'supersedes', 'character(64)', false, ''),
      ('territory_versions', 'schema', 'text', true, ''),
      ('territory_versions', 'record_fingerprint', 'character(64)', true, ''),
      ('territory_versions', 'record_document', 'jsonb', true, ''),
      ('territory_versions', 'import_id', 'bigint', true, ''),
      ('schema_migrations', 'version', 'text', true, ''),
      ('schema_migrations', 'body_sha256', 'character(64)', true, ''),
      ('schema_migrations', 'applied_at', 'timestamp with time zone', true, ''),
      ('schema_migrations', 'applied_by', 'text', true, ''),
      ('schema_migrations', 'channel', 'text', true, '')
    ), actual AS (
      SELECT c.relname::text, a.attname::text, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attidentity::text
      FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
      WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf') AND c.relkind = 'r'
        AND a.attnum > 0 AND NOT a.attisdropped
    ) SELECT 1 FROM ((SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)) difference
  ) THEN
    RAISE EXCEPTION 'GEONAF: colonnes post-0001 non conformes';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace = pg_catalog.to_regnamespace('geonaf')
             AND relkind = 'r' AND (NOT relrowsecurity OR relispartition))
     OR (SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_catalog.pg_class
         WHERE relnamespace = pg_catalog.to_regnamespace('geonaf') AND relkind = 'v')
        IS DISTINCT FROM 'territories_latest,territory_identities'
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace = pg_catalog.to_regnamespace('geonaf')
                AND relkind = 'v' AND NOT coalesce('security_invoker=true' = ANY(reloptions), false)) THEN
    RAISE EXCEPTION 'GEONAF: RLS ou vues post-0001 non conformes';
  END IF;
  LOCK TABLE geonaf.territories_latest, geonaf.territory_identities IN ACCESS EXCLUSIVE MODE;
  IF EXISTS (
    WITH expected(view_name, definition) AS (VALUES
      ('territories_latest', ' SELECT DISTINCT ON (territory_id) territory_id,
    version,
    kind,
    label,
    legal_status,
    legal_evidence,
    release_id,
    retrieved_at_utc,
    geometry_sha256,
    file_sha256,
    storage_key,
    wgs84_bounds,
    area_km2,
    vertices,
    parts,
    created_at_utc,
    country_iso3,
    admin_level_code,
    local_type,
    source_unit_id,
    parent_territory_id,
    grid_system,
    grid_cell_id,
    grid_cell_size_deg,
    scientific_profile_id,
    limits,
    supersedes,
    schema,
    record_fingerprint,
    record_document,
    import_id
   FROM geonaf.territory_versions
  ORDER BY territory_id, version DESC;'),
      ('territory_identities', ' SELECT territory_id,
    version,
    kind,
    geometry_sha256,
    file_sha256,
    record_fingerprint
   FROM geonaf.territory_versions;')
    ), actual AS (
      SELECT c.relname::text, pg_catalog.pg_get_viewdef(c.oid, false)
      FROM pg_catalog.pg_class c
      WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf') AND c.relkind = 'v'
    ) SELECT 1 FROM ((SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)) difference
  ) THEN
    RAISE EXCEPTION 'GEONAF: vues post-0001 non conformes';
  END IF;
  IF (SELECT string_agg(proname, ',' ORDER BY proname) FROM pg_catalog.pg_proc
      WHERE pronamespace = pg_catalog.to_regnamespace('geonaf'))
     IS DISTINCT FROM 'check_territory_version,close_import_only,forbid_change' THEN
    RAISE EXCEPTION 'GEONAF: fonctions post-0001 non conformes';
  END IF;
  -- Canonical catalog definitions from the unchanged SQL Editor application of 0001.
  -- Reviewed body SHA-256: 719e7e847f28404255491c0c00923f130cbd35151cd63488ed1ba3391c0bf971.
  -- pg_get_constraintdef / pg_get_viewdef are also used by the existing read-only diagnostics.
  -- Reference deparser: PostgreSQL 16.4 / PostGIS 3.6.2 in extensions, empty search_path.
  -- Exact text comparison fails closed on incompatible deparser output; never normalize expressions.
  IF EXISTS (
    WITH expected(table_name, constraint_name, definition) AS (VALUES
      ('data_sources', 'data_sources_access_tier_check', 'CHECK ((access_tier = ANY (ARRAY[''public''::text, ''restricted''::text, ''commercial''::text])))'),
      ('data_sources', 'data_sources_pkey', 'PRIMARY KEY (source_id)'),
      ('data_sources', 'data_sources_source_id_check', 'CHECK ((btrim(source_id) <> ''''::text))'),
      ('geometries', 'geometries_check', 'CHECK ((extensions.st_numgeometries(geom) = parts))'),
      ('geometries', 'geometries_check1', 'CHECK ((extensions.st_npoints(geom) = vertices))'),
      ('geometries', 'geometries_check2', 'CHECK (((source_geometry_type = ''MultiPolygon''::text) OR (parts = 1)))'),
      ('geometries', 'geometries_geom_check', 'CHECK ((extensions.st_isvalid(geom) AND (NOT extensions.st_isempty(geom))))'),
      ('geometries', 'geometries_geom_check1', 'CHECK (((extensions.st_xmin((geom)::extensions.box3d) >= (''-180''::integer)::double precision) AND (extensions.st_xmax((geom)::extensions.box3d) <= (180)::double precision) AND (extensions.st_ymin((geom)::extensions.box3d) >= (''-90''::integer)::double precision) AND (extensions.st_ymax((geom)::extensions.box3d) <= (90)::double precision)))'),
      ('geometries', 'geometries_geometry_sha256_check', 'CHECK ((geometry_sha256 ~ ''^[0-9a-f]{64}$''::text))'),
      ('geometries', 'geometries_identity_version_check', 'CHECK ((identity_version = ''geonaf-aoi-geometry-v1''::text))'),
      ('geometries', 'geometries_parts_check', 'CHECK ((parts >= 1))'),
      ('geometries', 'geometries_pkey', 'PRIMARY KEY (geometry_sha256)'),
      ('geometries', 'geometries_source_geometry_type_check', 'CHECK ((source_geometry_type = ANY (ARRAY[''Polygon''::text, ''MultiPolygon''::text])))'),
      ('geometries', 'geometries_vertices_check', 'CHECK ((vertices >= 4))'),
      ('legal_statuses', 'legal_statuses_kind_fkey', 'FOREIGN KEY (kind) REFERENCES geonaf.territory_kinds(kind)'),
      ('legal_statuses', 'legal_statuses_pkey', 'PRIMARY KEY (kind, legal_status)'),
      ('licences', 'licences_licence_id_check', 'CHECK ((licence_id ~ ''^[A-Za-z0-9.+-]+$''::text))'),
      ('licences', 'licences_name_check', 'CHECK ((btrim(name) <> ''''::text))'),
      ('licences', 'licences_pkey', 'PRIMARY KEY (licence_id)'),
      ('licences', 'licences_url_check', 'CHECK (((url IS NULL) OR (url ~ ''^https://''::text)))'),
      ('registry_imports', 'registry_imports_code_version_check', 'CHECK ((btrim(code_version) <> ''''::text))'),
      ('registry_imports', 'registry_imports_pkey', 'PRIMARY KEY (import_id)'),
      ('registry_imports', 'registry_imports_records_existing_check', 'CHECK ((records_existing >= 0))'),
      ('registry_imports', 'registry_imports_records_inserted_check', 'CHECK ((records_inserted >= 0))'),
      ('registry_imports', 'registry_imports_records_seen_check', 'CHECK ((records_seen >= 0))'),
      ('registry_imports', 'registry_imports_source_label_check', 'CHECK (((btrim(source_label) <> ''''::text) AND (source_label !~ ''[/\\:]''::text)))'),
      ('schema_migrations', 'schema_migrations_pkey', 'PRIMARY KEY (version)'),
      ('source_releases', 'source_releases_attribution_check', 'CHECK ((btrim(attribution) <> ''''::text))'),
      ('source_releases', 'source_releases_licence_id_fkey', 'FOREIGN KEY (licence_id) REFERENCES geonaf.licences(licence_id)'),
      ('source_releases', 'source_releases_pkey', 'PRIMARY KEY (release_id)'),
      ('source_releases', 'source_releases_product_check', 'CHECK ((btrim(product) <> ''''::text))'),
      ('source_releases', 'source_releases_reference_year_check', 'CHECK (((reference_year >= 1800) AND (reference_year <= 2200)))'),
      ('source_releases', 'source_releases_source_id_fkey', 'FOREIGN KEY (source_id) REFERENCES geonaf.data_sources(source_id)'),
      ('source_releases', 'source_releases_source_id_product_version_licence_id_attrib_key', 'UNIQUE NULLS NOT DISTINCT (source_id, product, version, licence_id, attribution, url, share_alike, reference_year)'),
      ('source_releases', 'source_releases_version_check', 'CHECK ((btrim(version) <> ''''::text))'),
      ('territories', 'territories_kind_fkey', 'FOREIGN KEY (kind) REFERENCES geonaf.territory_kinds(kind)'),
      ('territories', 'territories_pkey', 'PRIMARY KEY (territory_id)'),
      ('territories', 'territories_territory_id_check', 'CHECK ((territory_id ~ ''^[a-z0-9][a-z0-9_-]{1,63}$''::text))'),
      ('territories', 'territories_territory_id_kind_key', 'UNIQUE (territory_id, kind)'),
      ('territory_kinds', 'territory_kinds_pkey', 'PRIMARY KEY (kind)'),
      ('territory_versions', 'territory_versions_area_km2_check', 'CHECK ((area_km2 > (0)::double precision))'),
      ('territory_versions', 'territory_versions_check', 'CHECK (((legal_status <> ''officiel vérifié''::text) OR (length(btrim(COALESCE(legal_evidence, ''''::text))) >= 8)))'),
      ('territory_versions', 'territory_versions_check1', 'CHECK (((kind <> ALL (ARRAY[''acquisition_zone''::text, ''grid_cell''::text])) OR ((country_iso3 IS NULL) AND (admin_level_code IS NULL) AND (local_type IS NULL) AND (source_unit_id IS NULL) AND (parent_territory_id IS NULL))))'),
      ('territory_versions', 'territory_versions_check2', 'CHECK (((kind <> ALL (ARRAY[''acquisition_zone''::text, ''grid_cell''::text])) OR (lower(label) !~ ''(commune|département|departement|arrondissement|province|district|canton|préfecture|region|région|municipalit)''::text)))'),
      ('territory_versions', 'territory_versions_check3', 'CHECK (((kind <> ''admin_unit''::text) OR ((country_iso3 IS NOT NULL) AND (btrim(COALESCE(admin_level_code, ''''::text)) <> ''''::text) AND (btrim(COALESCE(local_type, ''''::text)) <> ''''::text))))'),
      ('territory_versions', 'territory_versions_check4', 'CHECK (((kind = ''grid_cell''::text) = ((grid_system IS NOT NULL) AND (grid_cell_id IS NOT NULL))))'),
      ('territory_versions', 'territory_versions_check5', 'CHECK (((version = 1) = (supersedes IS NULL)))'),
      ('territory_versions', 'territory_versions_check6', 'CHECK (((jsonb_typeof(record_document) = ''object''::text) AND ((record_document ->> ''territory_id''::text) = territory_id) AND (((record_document ->> ''version''::text))::integer = version) AND ((record_document ->> ''geometry_sha256''::text) = (geometry_sha256)::text) AND ((record_document ->> ''file_sha256''::text) = (file_sha256)::text)))'),
      ('territory_versions', 'territory_versions_country_iso3_check', 'CHECK ((country_iso3 ~ ''^[A-Z]{3}$''::text))'),
      ('territory_versions', 'territory_versions_file_sha256_check', 'CHECK ((file_sha256 ~ ''^[0-9a-f]{64}$''::text))'),
      ('territory_versions', 'territory_versions_geometry_sha256_fkey', 'FOREIGN KEY (geometry_sha256) REFERENCES geonaf.geometries(geometry_sha256)'),
      ('territory_versions', 'territory_versions_import_id_fkey', 'FOREIGN KEY (import_id) REFERENCES geonaf.registry_imports(import_id)'),
      ('territory_versions', 'territory_versions_kind_legal_status_fkey', 'FOREIGN KEY (kind, legal_status) REFERENCES geonaf.legal_statuses(kind, legal_status)'),
      ('territory_versions', 'territory_versions_label_check', 'CHECK ((btrim(label) <> ''''::text))'),
      ('territory_versions', 'territory_versions_parent_territory_id_fkey', 'FOREIGN KEY (parent_territory_id) REFERENCES geonaf.territories(territory_id) DEFERRABLE INITIALLY DEFERRED'),
      ('territory_versions', 'territory_versions_pkey', 'PRIMARY KEY (territory_id, version)'),
      ('territory_versions', 'territory_versions_record_fingerprint_check', 'CHECK ((record_fingerprint ~ ''^[0-9a-f]{64}$''::text))'),
      ('territory_versions', 'territory_versions_record_fingerprint_key', 'UNIQUE (record_fingerprint)'),
      ('territory_versions', 'territory_versions_release_id_fkey', 'FOREIGN KEY (release_id) REFERENCES geonaf.source_releases(release_id)'),
      ('territory_versions', 'territory_versions_schema_check', 'CHECK ((schema = ''geonaf.territory/v1''::text))'),
      ('territory_versions', 'territory_versions_storage_key_check', 'CHECK ((storage_key ~ ''^geometries/[0-9a-f]{64}[.]geojson$''::text))'),
      ('territory_versions', 'territory_versions_supersedes_check', 'CHECK (((supersedes IS NULL) OR (supersedes ~ ''^[0-9a-f]{64}$''::text)))'),
      ('territory_versions', 'territory_versions_territory_id_kind_fkey', 'FOREIGN KEY (territory_id, kind) REFERENCES geonaf.territories(territory_id, kind)'),
      ('territory_versions', 'territory_versions_version_check', 'CHECK ((version >= 1))'),
      ('territory_versions', 'territory_versions_wgs84_bounds_check', 'CHECK ((cardinality(wgs84_bounds) = 4))')
    ), actual AS (
      SELECT c.relname::text, k.conname::text, pg_catalog.pg_get_constraintdef(k.oid, false)
      FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid = k.conrelid
      WHERE k.connamespace = pg_catalog.to_regnamespace('geonaf')
    ) SELECT 1 FROM ((SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)) difference
  ) OR EXISTS (SELECT 1 FROM pg_catalog.pg_constraint
               WHERE connamespace = pg_catalog.to_regnamespace('geonaf') AND NOT convalidated) THEN
    RAISE EXCEPTION 'GEONAF: contraintes post-0001 non conformes';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
    JOIN pg_catalog.pg_am a ON a.oid = c.relam
    WHERE c.oid = pg_catalog.to_regclass('geonaf.geometries_geom_gist')
      AND a.amname = 'gist' AND i.indisvalid AND i.indisready
      AND i.indrelid = pg_catalog.to_regclass('geonaf.geometries')) THEN
    RAISE EXCEPTION 'GEONAF: index spatial post-0001 absent ou invalide';
  END IF;
  IF EXISTS (
    WITH expected(index_name, definition) AS (VALUES
      ('geometries_geom_gist', 'CREATE INDEX geometries_geom_gist ON geonaf.geometries USING gist (geom)'),
      ('territory_versions_geometry', 'CREATE INDEX territory_versions_geometry ON geonaf.territory_versions USING btree (geometry_sha256)'),
      ('territory_versions_file', 'CREATE INDEX territory_versions_file ON geonaf.territory_versions USING btree (file_sha256)'),
      ('territory_versions_release', 'CREATE INDEX territory_versions_release ON geonaf.territory_versions USING btree (release_id)'),
      ('territory_versions_admin', 'CREATE INDEX territory_versions_admin ON geonaf.territory_versions USING btree (country_iso3, admin_level_code)')
    ), actual AS (
      SELECT c.relname::text, pg_catalog.pg_get_indexdef(c.oid)
      FROM pg_catalog.pg_class c JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
      WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf')
        AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conindid = c.oid AND k.contype IN ('p','u'))
    ) SELECT 1 FROM ((SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)) difference
  ) OR EXISTS (SELECT 1 FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
               WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf') AND (NOT i.indisvalid OR NOT i.indisready))
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace = pg_catalog.to_regnamespace('geonaf')
                AND relkind NOT IN ('r','v','i','S'))
     OR (SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_catalog.pg_class
         WHERE relnamespace = pg_catalog.to_regnamespace('geonaf') AND relkind = 'S')
        IS DISTINCT FROM 'registry_imports_import_id_seq,source_releases_release_id_seq' THEN
    RAISE EXCEPTION 'GEONAF: index, sequences ou objets supplementaires non conformes';
  END IF;
  SELECT count(*) = 1 AND coalesce(bool_and(
    version = '0001_territorial_registry'
    AND body_sha256 = '719e7e847f28404255491c0c00923f130cbd35151cd63488ed1ba3391c0bf971'
    AND channel = 'supabase-sql-editor' AND applied_at IS NOT NULL AND applied_by <> ''), false)
  INTO history_ok FROM geonaf.schema_migrations;
  IF history_ok IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'GEONAF: historique 0001 absent ou incoherent (version, empreinte, canal)';
  END IF;
  IF EXISTS (SELECT 1 FROM geonaf.registry_imports) OR EXISTS (SELECT 1 FROM geonaf.territory_versions)
     OR EXISTS (SELECT 1 FROM geonaf.territories) OR EXISTS (SELECT 1 FROM geonaf.geometries)
     OR EXISTS (SELECT 1 FROM geonaf.data_sources) OR EXISTS (SELECT 1 FROM geonaf.source_releases)
     OR (SELECT count(*) FROM geonaf.licences) <> 3
     OR (SELECT count(*) FROM geonaf.legal_statuses) <> 7
     OR (SELECT count(*) FROM geonaf.territory_kinds) <> 4 THEN
    RAISE EXCEPTION 'GEONAF: donnees incompatibles avec un marquage immediatement apres 0001';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc WHERE oid = pg_catalog.to_regprocedure('geonaf.forbid_change()')
      AND prosrc = '
BEGIN
  RAISE EXCEPTION ''GEONAF: changement interdit, les enregistrements sont immuables''
    USING DETAIL = TG_OP || '' sur '' || TG_TABLE_NAME;
END ' AND prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
      AND NOT prosecdef AND proconfig = ARRAY['search_path=""']) THEN
    RAISE EXCEPTION 'GEONAF: fonction post-0001 non conforme';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc WHERE oid = pg_catalog.to_regprocedure('geonaf.close_import_only()')
      AND prosrc = '
BEGIN
  IF TG_OP <> ''UPDATE'' OR OLD.finished_at IS NOT NULL OR NEW.finished_at IS NULL
     OR NEW.import_id <> OLD.import_id OR NEW.started_at <> OLD.started_at
     OR NEW.source_label <> OLD.source_label OR NEW.code_version <> OLD.code_version THEN
    RAISE EXCEPTION ''GEONAF: le journal d''''import ne peut être que clôturé une fois''
      USING DETAIL = TG_OP || '' sur registry_imports'';
  END IF;
  RETURN NEW;
END ' AND prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
      AND NOT prosecdef AND proconfig = ARRAY['search_path=""']) THEN
    RAISE EXCEPTION 'GEONAF: fonction post-0001 non conforme';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc WHERE oid = pg_catalog.to_regprocedure('geonaf.check_territory_version()')
      AND prosrc = '
DECLARE
  previous_fingerprint char(64);
  stored_parts integer;
  stored_vertices integer;
BEGIN
  IF NEW.version > 1 THEN
    SELECT record_fingerprint INTO previous_fingerprint FROM geonaf.territory_versions
     WHERE territory_id = NEW.territory_id AND version = NEW.version - 1;
    IF previous_fingerprint IS NULL OR previous_fingerprint <> NEW.supersedes THEN
      RAISE EXCEPTION ''GEONAF: version sans version précédente correspondante''
        USING DETAIL = NEW.territory_id || '' v'' || NEW.version;
    END IF;
  END IF;
  SELECT parts, vertices INTO stored_parts, stored_vertices FROM geonaf.geometries
   WHERE geometry_sha256 = NEW.geometry_sha256;
  IF stored_parts IS DISTINCT FROM NEW.parts OR stored_vertices IS DISTINCT FROM NEW.vertices THEN
    RAISE EXCEPTION ''GEONAF: parties ou sommets incohérents avec la géométrie enregistrée''
      USING DETAIL = NEW.territory_id || '' v'' || NEW.version;
  END IF;
  RETURN NEW;
END ' AND prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
      AND NOT prosecdef AND proconfig = ARRAY['search_path=""']) THEN
    RAISE EXCEPTION 'GEONAF: fonction post-0001 non conforme';
  END IF;
  IF EXISTS (
    WITH expected(table_name, trigger_name, trigger_type, function_name) AS (VALUES
      ('licences', 'licences_immutable', 27, 'forbid_change'),
      ('licences', 'licences_no_truncate', 34, 'forbid_change'),
      ('data_sources', 'data_sources_immutable', 27, 'forbid_change'),
      ('data_sources', 'data_sources_no_truncate', 34, 'forbid_change'),
      ('source_releases', 'source_releases_immutable', 27, 'forbid_change'),
      ('source_releases', 'source_releases_no_truncate', 34, 'forbid_change'),
      ('geometries', 'geometries_immutable', 27, 'forbid_change'),
      ('geometries', 'geometries_no_truncate', 34, 'forbid_change'),
      ('territory_kinds', 'territory_kinds_immutable', 27, 'forbid_change'),
      ('territory_kinds', 'territory_kinds_no_truncate', 34, 'forbid_change'),
      ('legal_statuses', 'legal_statuses_immutable', 27, 'forbid_change'),
      ('legal_statuses', 'legal_statuses_no_truncate', 34, 'forbid_change'),
      ('territories', 'territories_immutable', 27, 'forbid_change'),
      ('territories', 'territories_no_truncate', 34, 'forbid_change'),
      ('territory_versions', 'territory_versions_immutable', 27, 'forbid_change'),
      ('territory_versions', 'territory_versions_no_truncate', 34, 'forbid_change'),
      ('registry_imports', 'registry_imports_close', 27, 'close_import_only'),
      ('registry_imports', 'registry_imports_no_truncate', 34, 'forbid_change'),
      ('territory_versions', 'territory_versions_check', 7, 'check_territory_version'),
      ('schema_migrations', 'schema_migrations_immutable', 27, 'forbid_change'),
      ('schema_migrations', 'schema_migrations_no_truncate', 34, 'forbid_change')
    ), actual AS (
      SELECT c.relname::text, t.tgname::text, t.tgtype::integer, p.proname::text
      FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
      JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
      WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf') AND NOT t.tgisinternal
        AND t.tgenabled = 'O' AND p.pronamespace = c.relnamespace
        AND t.tgqual IS NULL AND t.tgnargs = 0
    ) SELECT 1 FROM ((SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)) difference
  ) THEN
    RAISE EXCEPTION 'GEONAF: declencheurs post-0001 non conformes';
  END IF;
  IF EXISTS ((SELECT licence_id, name, url, share_alike FROM geonaf.licences EXCEPT VALUES ('CC-BY-3.0', 'Creative Commons Attribution 3.0 Unported',
   'https://creativecommons.org/licenses/by/3.0/', false),
  ('CC-BY-3.0-IGO', 'Creative Commons Attribution 3.0 IGO',
   'https://creativecommons.org/licenses/by/3.0/igo/', false),
  ('LicenseRef-GEONAF', 'Zones de travail GEONAF', NULL, false))
    UNION ALL (VALUES ('CC-BY-3.0', 'Creative Commons Attribution 3.0 Unported',
   'https://creativecommons.org/licenses/by/3.0/', false),
  ('CC-BY-3.0-IGO', 'Creative Commons Attribution 3.0 IGO',
   'https://creativecommons.org/licenses/by/3.0/igo/', false),
  ('LicenseRef-GEONAF', 'Zones de travail GEONAF', NULL, false) EXCEPT SELECT licence_id, name, url, share_alike FROM geonaf.licences)) THEN
    RAISE EXCEPTION 'GEONAF: referentiel initial post-0001 non conforme';
  END IF;
  IF EXISTS ((SELECT kind FROM geonaf.territory_kinds EXCEPT VALUES ('acquisition_zone'), ('admin_unit'), ('custom_import'), ('grid_cell'))
    UNION ALL (VALUES ('acquisition_zone'), ('admin_unit'), ('custom_import'), ('grid_cell') EXCEPT SELECT kind FROM geonaf.territory_kinds)) THEN
    RAISE EXCEPTION 'GEONAF: referentiel initial post-0001 non conforme';
  END IF;
  IF EXISTS ((SELECT kind, legal_status FROM geonaf.legal_statuses EXCEPT VALUES ('acquisition_zone', 'zone d''acquisition GEONAF'),
  ('admin_unit', 'référentiel communautaire'),
  ('admin_unit', 'référentiel humanitaire (COD)'),
  ('admin_unit', 'officiel vérifié'),
  ('custom_import', 'fourni par l''utilisateur'),
  ('custom_import', 'officiel vérifié'),
  ('grid_cell', 'grille GEONAF'))
    UNION ALL (VALUES ('acquisition_zone', 'zone d''acquisition GEONAF'),
  ('admin_unit', 'référentiel communautaire'),
  ('admin_unit', 'référentiel humanitaire (COD)'),
  ('admin_unit', 'officiel vérifié'),
  ('custom_import', 'fourni par l''utilisateur'),
  ('custom_import', 'officiel vérifié'),
  ('grid_cell', 'grille GEONAF') EXCEPT SELECT kind, legal_status FROM geonaf.legal_statuses)) THEN
    RAISE EXCEPTION 'GEONAF: referentiel initial post-0001 non conforme';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
             WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf') AND NOT t.tgisinternal
               AND (t.tgenabled <> 'O' OR t.tgqual IS NOT NULL OR t.tgnargs <> 0)) THEN
    RAISE EXCEPTION 'GEONAF: declencheur desactive ou conditionnel non conforme';
  END IF;
  IF EXISTS (
    WITH expected(table_name, column_name, definition) AS (VALUES
      ('data_sources', 'access_tier', '''public''::text'),
      ('registry_imports', 'started_at', 'now()'),
      ('registry_imports', 'records_seen', '0'),
      ('registry_imports', 'records_inserted', '0'),
      ('registry_imports', 'records_existing', '0'),
      ('territory_versions', 'limits', '''{}''::text[]'),
      ('schema_migrations', 'applied_at', 'now()'),
      ('schema_migrations', 'applied_by', 'CURRENT_USER')
    ), actual AS (
      SELECT c.relname::text, a.attname::text, pg_catalog.pg_get_expr(d.adbin, d.adrelid)
      FROM pg_catalog.pg_attrdef d JOIN pg_catalog.pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
      JOIN pg_catalog.pg_class c ON c.oid = d.adrelid
      WHERE c.relnamespace = pg_catalog.to_regnamespace('geonaf')
    ) SELECT 1 FROM ((SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL (SELECT * FROM actual EXCEPT SELECT * FROM expected)) difference
  ) THEN
    RAISE EXCEPTION 'GEONAF: valeurs par defaut post-0001 non conformes';
  END IF;
  IF confirmation_i2 IS DISTINCT FROM true OR confirmation_instance IS DISTINCT FROM
     pg_catalog.to_char(pg_catalog.pg_postmaster_start_time() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') THEN
    RAISE EXCEPTION 'GEONAF: confirmation I2 privee et continuite instance requises, marquage refuse';
  END IF;
  COMMENT ON SCHEMA geonaf IS 'geonaf-environment:test';
END $mark$;

SELECT pg_catalog.obj_description(pg_catalog.to_regnamespace('geonaf'), 'pg_namespace') AS environnement;
