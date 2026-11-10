-- GEONAF - Supabase SQL Editor - 01 roles (idempotent; run once per project, before the migration).
--
-- Supabase: one database per project ("postgres"), migrations run as the postgres role of the SQL Editor,
-- which owns the geonaf schema. Two GEONAF roles are created WITHOUT LOGIN and without password: no
-- direct PostgreSQL connection is possible from the workstation today, and a password typed here would
-- be kept in the editor's history. LOGIN and passwords are added later, outside the editor.
--   geonaf_reader   : read only (application, analyses).
--   geonaf_importer : reads, inserts registry rows, closes its own import journal entry.
-- Grants and row level security policies for these roles are created by the migration.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'geonaf_reader') THEN
    CREATE ROLE geonaf_reader NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'geonaf_importer') THEN
    CREATE ROLE geonaf_importer NOLOGIN;
  END IF;
END $$;

SELECT rolname, rolcanlogin, rolsuper, rolbypassrls FROM pg_roles
WHERE rolname IN ('geonaf_reader', 'geonaf_importer') ORDER BY rolname;
