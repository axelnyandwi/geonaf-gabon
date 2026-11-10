-- GEONAF - Supabase SQL Editor - 03 mark this project as the "dev" environment (after the migration).
-- Run ONLY on project geonaf-dev. The mark is a comment on the geonaf schema; destructive scripts
-- (90, 99) refuse to run unless it reads geonaf-environment:test. A mark is never changed.
DO $$
DECLARE current_mark text := obj_description(to_regnamespace('geonaf'), 'pg_namespace');
BEGIN
  IF to_regnamespace('geonaf') IS NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf absent, appliquer la migration avant de marquer le projet';
  END IF;
  IF current_mark IS NOT NULL AND current_mark <> 'geonaf-environment:dev' THEN
    RAISE EXCEPTION 'GEONAF: projet deja marque comme autre environnement' USING DETAIL = current_mark;
  END IF;
END $$;
COMMENT ON SCHEMA geonaf IS 'geonaf-environment:dev';

SELECT obj_description(to_regnamespace('geonaf'), 'pg_namespace') AS environnement;
