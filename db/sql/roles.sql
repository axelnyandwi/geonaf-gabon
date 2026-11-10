-- GEONAF roles, created once per PostgreSQL server by an administrator (not by a migration: roles are
-- server-wide). Passwords are never written here: set them interactively with \password in psql.
--
--   geonaf_migrator : owns the geonaf schema, applies Alembic migrations.
--   geonaf_importer : reads everything, inserts registry rows, closes its import journal entry.
--   geonaf_reader   : reads only (application, analyses).
--
-- Grants on the tables are given by the migrations themselves (end of 0001).

CREATE ROLE geonaf_migrator LOGIN;
CREATE ROLE geonaf_importer LOGIN;
CREATE ROLE geonaf_reader LOGIN;

-- Development database and test database (the integration tests refuse a database whose name does
-- not end with "_test", and drop/recreate the geonaf schema there).
CREATE DATABASE geonaf OWNER geonaf_migrator;
CREATE DATABASE geonaf_test OWNER geonaf_migrator;

-- Then, connected to each database as an administrator:
--   CREATE EXTENSION IF NOT EXISTS postgis;
--   GRANT CONNECT ON DATABASE <database> TO geonaf_importer, geonaf_reader;
