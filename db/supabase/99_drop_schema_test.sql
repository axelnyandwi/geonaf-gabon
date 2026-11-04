-- GEONAF - Supabase SQL Editor - 99 drop the geonaf schema. PROJECT geonaf-test ONLY.
--
-- Destructive: removes every GEONAF table, view, function and row of the project. Refuses to run unless
-- the schema is marked geonaf-environment:test. The roles (01_roles.sql) and PostGIS are left in place.
-- The file registry (data/territories) is not affected: a new import restores the content.
DO $$
BEGIN
  IF coalesce(obj_description(to_regnamespace('geonaf'), 'pg_namespace'), '') <> 'geonaf-environment:test' THEN
    RAISE EXCEPTION 'GEONAF: suppression reservee au projet de test (schema geonaf non marque geonaf-environment:test)';
  END IF;
END $$;
DROP SCHEMA geonaf CASCADE;

SELECT to_regnamespace('geonaf') IS NULL AS schema_supprime;
