-- GEONAF migration 0001, downgrade. Local development and test databases only: it removes the
-- whole registry copy. The file registry (data/territories) is the reference and is not touched.
-- The PostGIS extension is left installed (other databases or schemas may use it).

DROP SCHEMA geonaf CASCADE;
