"""Storage layer (D3-D4) against a real LOCAL PostgreSQL (marker ``postgis``); never Supabase.

Needs ``GEONAF_TEST_DATABASE_URL`` on a local database whose name ends with ``_test`` (the ``geonaf`` schema is
dropped and recreated there). Two levels, each skip names what is missing:

* PostgreSQL only: guarded sessions on a real server (environment mark, missing PostGIS, read only,
  connection error without secret);
* PostGIS available (``CREATE EXTENSION postgis`` done by an administrator): migrations 0001 and 0002 with
  Alembic, idempotent imports of acquisitions and indicator results, parity between the file and PostGIS
  stores, R5 bis against a shadowing function, GiST index usable by the spatial queries.
Destructive fixtures additionally require GEONAF_ALLOW_DESTRUCTIVE_TESTS=1, an exact
GEONAF_TEST_DATABASE_NAME and GEONAF_TEST_INSTANCE_ID matching the database comment.
See tests/postgis_support.py; a _test suffix alone never authorizes access.
"""

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from src.data import observations as obs
from src.data import postgis_observations as pgo
from src.data import postgis_territory_registry as pg
from src.data import storage
from src.data.database import DatabaseConnectionError, EnvironmentIdentityError, parse_target
from src.data.postgis_session import PostgisSessions, PostgisUnavailableError
from tests.test_observations import acquisitions, make_indicator, zone_file  # noqa: F401  (fixture)
from tests.test_postgis_territory_unit import assert_parity, registry  # noqa: F401  (fixture)

from tests.postgis_support import isolated_connection, configure_alembic

ROOT = Path(__file__).resolve().parents[1]
TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
pytestmark = [pytest.mark.postgis,
              pytest.mark.skipif(not TEST_URL, reason="GEONAF_TEST_DATABASE_URL non défini : base locale requise.")]


def connect(*, autocommit=False):
    return isolated_connection(TEST_URL, autocommit=autocommit)


def has_postgis() -> bool:
    with connect(autocommit=True) as c:
        return c.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'").fetchone() is not None


@pytest.fixture
def local():
    pytest.importorskip("psycopg")
    with connect(autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
    yield
    with connect(autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")


def sessions(environment="test", url=None):
    target = parse_target(url or TEST_URL.replace("postgresql://", "postgresql+psycopg://", 1), environment)
    return PostgisSessions(target)


# --- PostgreSQL only --------------------------------------------------------------------------------


def test_session_refuses_an_unmarked_database_before_any_query(local):
    with connect(autocommit=True) as c:
        c.execute("CREATE SCHEMA geonaf")
    with pytest.raises(EnvironmentIdentityError):
        with sessions().read():
            pytest.fail("connexion remise sans marque")


def test_session_on_a_marked_database_without_postgis_says_so(local):
    if has_postgis():
        pytest.skip("PostGIS présent : cas « PostGIS absent » non reproductible sur ce serveur.")
    with connect(autocommit=True) as c:
        c.execute("CREATE SCHEMA geonaf")
        c.execute("COMMENT ON SCHEMA geonaf IS 'geonaf-environment:test'")
    with pytest.raises(PostgisUnavailableError):
        with sessions().read():
            pass


def test_connection_error_names_no_host_user_or_password(local):
    target = parse_target(TEST_URL, "test")
    url = f"postgresql+psycopg://geonaf_reader:Secret-Value-123@127.0.0.1:1/{target.database}"
    with pytest.raises(DatabaseConnectionError) as caught:
        with sessions(url=url).read():
            pass
    assert "Secret-Value-123" not in str(caught.value) and "127.0.0.1" not in str(caught.value)
    assert "geonaf_reader" not in str(caught.value)


# --- PostGIS ---------------------------------------------------------------------------------------


@pytest.fixture
def migrated(local, monkeypatch):
    """geonaf schema created by Alembic (0001 then 0002) and marked as the test environment."""
    if not has_postgis():
        pytest.skip("PostGIS indisponible sur le serveur local : migrations 0001/0002 et lectures spatiales "
                    "NON exécutées.")
    pytest.importorskip("alembic")
    from alembic import command
    from alembic.config import Config

    configure_alembic(monkeypatch, TEST_URL)
    with connect(autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS public.alembic_version")
    config = Config(str(ROOT / "db" / "alembic.ini"))
    command.upgrade(config, "head")
    with connect(autocommit=True) as c:
        c.execute("COMMENT ON SCHEMA geonaf IS 'geonaf-environment:test'")
    yield config
    with connect(autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS public.alembic_version")


def test_alembic_0002_downgrades_to_0001_and_upgrades_again(migrated):
    from alembic import command

    command.downgrade(migrated, "0001_territorial_registry")
    with connect() as c:
        assert c.execute("SELECT to_regclass('geonaf.acquisitions')").fetchone()[0] is None
        assert c.execute("SELECT to_regclass('geonaf.territory_versions')").fetchone()[0] is not None
    command.upgrade(migrated, "head")
    with connect() as c:
        assert c.execute("SELECT count(*) FROM geonaf.indicator_result_kinds").fetchone()[0] == 4


def test_acquisition_import_is_idempotent_journaled_and_immutable(migrated, acquisitions):  # noqa: F811
    import psycopg

    records = obs.FileAcquisitionCatalog(acquisitions).scan().records
    with connect() as c:
        dry = pgo.apply_acquisition_import(c, records, source_label="lot de test", code_version="t", expected_environment="test")
        assert (dry.applied, dry.inserted, dry.import_id) == (False, 2, None)
        assert c.execute("SELECT count(*) FROM geonaf.acquisition_manifests").fetchone()[0] == 0
        first = pgo.apply_acquisition_import(c, records, source_label="lot de test", code_version="t", expected_environment="test", apply=True)
        again = pgo.apply_acquisition_import(c, records, source_label="lot de test", code_version="t", expected_environment="test", apply=True)
        assert (first.inserted, again.inserted, again.existing) == (2, 0, 2)
        journal = c.execute("SELECT scope, records_seen, records_inserted, finished_at IS NOT NULL "
                            "FROM geonaf.data_imports ORDER BY import_id").fetchall()
        assert journal == [("acquisitions", 2, 2, True), ("acquisitions", 2, 0, True)]
        with pytest.raises(psycopg.errors.RaiseException), c.transaction():
            c.execute("UPDATE geonaf.acquisitions SET acquired_at = now()")
    catalog = pgo.PostgisAcquisitionCatalog(sessions=sessions())
    assert catalog.scan().records == records
    assert catalog.intersecting((8.9, 0.8, 9.1, 1.0)) == obs.FileAcquisitionCatalog(acquisitions).intersecting(
        (8.9, 0.8, 9.1, 1.0))


def test_indicator_import_needs_the_geometry_then_reads_back_identically(migrated, registry, tmp_path):  # noqa: F811
    from src.geo.aoi_identity import load_aoi_identity

    record = registry.territories()[0]
    zone = registry.geometry_path(record)
    store = obs.IndicatorResultStore(tmp_path / "data")
    result = store.save(make_indicator(load_aoi_identity(zone).file_sha256), zone, code_version="t",
                        computed_at=datetime(2026, 11, 5, tzinfo=timezone.utc))
    with connect() as c:
        with pytest.raises(pgo.ObservationImportError, match="registre de territoires"):
            pgo.apply_indicator_import(c, [result], source_label="lot", code_version="t", expected_environment="test", apply=True)
        pg.apply_import(c, pg.plan_import(registry), source_label="registre", code_version="t", expected_environment="test", apply=True)
        assert pgo.apply_indicator_import(c, [result], source_label="lot", code_version="t", expected_environment="test", apply=True).inserted == 1
        assert pgo.apply_indicator_import(c, [result], source_label="lot", code_version="t", expected_environment="test", apply=True).existing == 1
    assert pgo.PostgisIndicatorStore(sessions=sessions()).results() == store.results()


def test_postgis_store_reads_what_the_file_store_reads(migrated, registry, monkeypatch):  # noqa: F811
    with connect() as c:
        pg.apply_import(c, pg.plan_import(registry), source_label="registre", code_version="t", expected_environment="test", apply=True)
    environ = {"GEONAF_DB_BACKEND": "postgres", "GEONAF_DB_ENV": "test",
               "GEONAF_DATABASE_URL": TEST_URL.replace("postgresql://", "postgresql+psycopg://", 1)}
    store = storage.open_store(registry.data_dir, environ)
    assert store.is_postgis
    assert_parity(registry, store.territories)
    from src.services import territory_catalog as catalog

    files = catalog.admin_territories(registry.data_dir, "gbOpen", "ADM2", registry=registry)
    rows = catalog.admin_territories(registry.data_dir, "gbOpen", "ADM2", registry=store.territories)
    assert [catalog.selection_payload(o) for o in rows] == [catalog.selection_payload(o) for o in files]


def test_r5_bis_a_shadowing_function_on_the_default_path_is_never_used(migrated, registry):  # noqa: F811
    with connect() as c:
        pg.apply_import(c, pg.plan_import(registry), source_label="registre", code_version="t", expected_environment="test", apply=True)
    with connect(autocommit=True) as c:
        c.execute("CREATE SCHEMA IF NOT EXISTS geonaf_shadow")
        c.execute("CREATE OR REPLACE FUNCTION geonaf_shadow.st_makeenvelope(float8, float8, float8, float8, integer) "
                  "RETURNS geometry LANGUAGE sql AS 'SELECT NULL::geometry'")
    try:
        def shadowed():
            connection = connect(autocommit=True)
            try:
                connection.execute("SET search_path = geonaf_shadow, public, extensions")
                connection.autocommit = False
                return connection
            except BaseException:
                connection.close()
                raise
        with shadowed() as c:     # Without R5 bis the shadow wins: the envelope is NULL, nothing intersects.
            assert c.execute("SELECT st_makeenvelope(0, 0, 1, 1, 4326) IS NULL").fetchone()[0] is True
        database = pg.PostgisTerritoryRegistry(shadowed, registry.data_dir)
        bounds = (9.39, 0.39, 9.41, 0.41)
        assert [r.territory_id for r in database.intersecting(bounds)] == \
               [r.territory_id for r in registry.intersecting(bounds)] != []
    finally:
        with connect(autocommit=True) as c:
            c.execute("DROP SCHEMA geonaf_shadow CASCADE")


def test_spatial_queries_can_use_the_gist_indexes(migrated):
    with connect() as c:
        c.execute("SET LOCAL enable_seqscan = off")
        for query in (pg.QUERIES["latest_intersecting"], pgo.QUERIES["manifests_intersecting"]):
            plan = "\n".join(row[0] for row in c.execute(
                "EXPLAIN " + query, {"west": 9, "south": 0, "east": 10, "north": 1, "kind": None}).fetchall())
            assert "gist" in plan.lower() or "Index" in plan, plan


# --- Migration 0002 on PostgreSQL without PostGIS (explicit stand-ins) -----------------------------------

STAND_INS = (  # Only what PostGIS provides; every other line of 0002 runs unchanged.
    ("geometry(Polygon, 4326)", "bytea"),
    ("ST_IsValid(footprint) AND NOT ST_IsEmpty(footprint)", "length(footprint) > 0"),
    ("USING gist (footprint)", "(footprint)"),
)
ROLES = ("geonaf_reader", "geonaf_importer")


@pytest.fixture
def stand_in_0002(local):
    """0001's objects that 0002 needs (schema, forbid_change, geometries) then 0002 with the stand-ins.

    Proves on a real server: syntax, constraints, immutability triggers, journal trigger, view, RLS and
    grants of 0002. Proves nothing spatial (footprint type, GiST, ST_IsValid): see the PostGIS tests above.
    """
    body_0001 = (ROOT / "db" / "sql" / "0001_territorial_registry.up.sql").read_text(encoding="utf-8")
    forbid = body_0001[body_0001.index("CREATE FUNCTION geonaf.forbid_change()"):]
    forbid = forbid[:forbid.index("END $$;") + len("END $$;")]
    sql = (ROOT / "db" / "sql" / "0002_observations.up.sql").read_text(encoding="utf-8")
    for old, new in STAND_INS:
        assert old in sql
        sql = sql.replace(old, new)
    created = []
    with connect(autocommit=True) as c:
        for role in ROLES:
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                c.execute(f"CREATE ROLE {role} NOLOGIN")
                created.append(role)
        c.execute("DROP DOMAIN IF EXISTS public.geometry")
        c.execute("CREATE DOMAIN public.geometry AS bytea")          # Only for the precondition to_regtype().
        c.execute("CREATE SCHEMA geonaf")
        c.execute(forbid)
        c.execute("CREATE TABLE geonaf.geometries (geometry_sha256 char(64) PRIMARY KEY)")
        c.execute("GRANT USAGE ON SCHEMA geonaf TO geonaf_reader, geonaf_importer")        # As 0001 does.
        c.execute(sql)
    yield
    with connect(autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        c.execute("DROP DOMAIN IF EXISTS public.geometry")
        for role in created:
            c.execute(f"DROP OWNED BY {role}")
            c.execute(f"DROP ROLE {role}")


def _manifest_row(sha, ingested, product="S2B_X", acquired="2025-01-25T09:31:59+00"):
    document = ('{"product_id": "' + product + '", "collection": "sentinel-2-l2a", '
                '"schema_version": "geonaf.sentinel2-acquisition/v1"}')
    return (sha, "sentinel-2-l2a", product, "geonaf.sentinel2-acquisition/v1", acquired, ingested,
            "https://stac.example/v1", "EPSG:32632", 10.0, document)


INSERT_MANIFEST = ("INSERT INTO geonaf.acquisition_manifests (manifest_sha256, collection, product_id, schema_version, "
                   "acquired_at, ingested_at, stac_source, grid_crs, grid_resolution_m, manifest_document, import_id) "
                   "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)")
INSERT_RESULT = ("INSERT INTO geonaf.indicator_results (result_sha256, definition_sha256, geometry_sha256, aoi_id, "
                 "aoi_file_sha256, status, value, unit, reliability, computed_at, code_version, result_document, "
                 "import_id) VALUES (%s, %s, %s, 'akanda', %s, %s, %s, 'km²', 'moyenne', now(), 't', %s::jsonb, %s)")


def _result_document(geometry: str) -> str:
    return ('{"indicator": {"indicator_id": "v"}, "geometry_sha256": "' + geometry + '", '
            '"definition_sha256": "' + "f" * 64 + '"}')


def test_migration_0002_constraints_triggers_and_view_on_a_real_server(stand_in_0002):
    import psycopg

    with connect(autocommit=True) as c:
        journal = c.execute("INSERT INTO geonaf.data_imports (scope, source_label, code_version) "
                            "VALUES ('acquisitions', 'lot', 't') RETURNING import_id").fetchone()[0]
        c.execute("INSERT INTO geonaf.acquisitions VALUES ('sentinel-2-l2a', 'S2B_X', '2025-01-25T09:31:59+00')")
        c.execute(INSERT_MANIFEST, _manifest_row("a" * 64, "2025-01-26T12:00:00+00") + (journal,))
        c.execute(INSERT_MANIFEST, _manifest_row("b" * 64, "2025-02-01T00:00:00+00") + (journal,))
        assert c.execute("SELECT manifest_sha256 FROM geonaf.acquisitions_latest").fetchall() == [("b" * 64,)]
        # Equal ingestion dates: the greatest digest wins, as in AcquisitionScan.latest() (audit, parity).
        c.execute(INSERT_MANIFEST, _manifest_row("e" * 64, "2025-02-01T00:00:00+00") + (journal,))
        assert c.execute("SELECT manifest_sha256 FROM geonaf.acquisitions_latest").fetchall() == [("e" * 64,)]
        refused = [
            ("UPDATE geonaf.acquisitions SET acquired_at = now()", None, "immuables"),
            ("DELETE FROM geonaf.acquisition_manifests", None, "immuables"),
            ("TRUNCATE geonaf.acquisitions CASCADE", None, "immuables"),
            (INSERT_MANIFEST, _manifest_row("c" * 64, "2025-01-26T12:00:00+00", acquired="2025-01-24T00:00:00+00")
             + (journal,), "date d'acquisition"),
            (INSERT_MANIFEST, _manifest_row("d" * 64, "2025-01-01T00:00:00+00") + (journal,), "check"),
            ("UPDATE geonaf.data_imports SET scope = 'indicators', finished_at = now()", None, "clôturé"),
        ]
        for statement, params, fragment in refused:
            with pytest.raises(psycopg.Error) as caught:
                c.execute(statement, params)
            assert fragment.lower() in str(caught.value).lower(), statement
        c.execute("UPDATE geonaf.data_imports SET finished_at = now(), records_seen = 2")      # Closed once...
        with pytest.raises(psycopg.Error, match="clôturé"):
            c.execute("UPDATE geonaf.data_imports SET records_seen = 3")                        # ...never twice.
        c.execute("INSERT INTO geonaf.geometries VALUES (%s)", ("e" * 64,))
        definition = '{"indicator_id": "v", "profile_fingerprint": "' + "0" * 64 + '"}'
        c.execute("INSERT INTO geonaf.indicator_definitions VALUES (%s, 'v', 'V', 'km²', 'm', 'estimation', "
                  "'experimental', %s, %s::jsonb)", ("f" * 64, "0" * 64, definition))
        for status, value in (("disponible", float("nan")), ("disponible", float("inf")), ("indisponible", 1.0)):
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(INSERT_RESULT, ("1" * 64, "f" * 64, "e" * 64, "2" * 64, status, value,
                                          _result_document("e" * 64), journal))
        c.execute(INSERT_RESULT, ("1" * 64, "f" * 64, "e" * 64, "2" * 64, "disponible", 3.25,
                                  _result_document("e" * 64), journal))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):    # Never attached to an unknown geometry.
            c.execute(INSERT_RESULT, ("3" * 64, "f" * 64, "9" * 64, "2" * 64, "disponible", 1.0,
                                      _result_document("9" * 64), journal))


def test_migration_0002_privileges_and_row_level_security(stand_in_0002):
    import psycopg

    with connect(autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM pg_class WHERE relnamespace = 'geonaf'::regnamespace AND relkind = 'r' "
                         "AND relname <> 'geometries' AND NOT relrowsecurity").fetchone()[0] == 0
        c.execute("SET ROLE geonaf_reader")
        assert c.execute("SELECT count(*) FROM geonaf.indicator_result_kinds").fetchone()[0] == 4
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO geonaf.data_imports (scope, source_label, code_version) "
                      "VALUES ('indicators', 'l', 't')")
        c.execute("RESET ROLE")
        c.execute("SET ROLE geonaf_importer")
        c.execute("INSERT INTO geonaf.data_imports (scope, source_label, code_version) VALUES ('indicators', 'l', 't')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO geonaf.indicator_result_kinds VALUES ('autre')")
        c.execute("RESET ROLE")


def test_migration_0002_downgrade_removes_only_its_objects(stand_in_0002):
    with connect(autocommit=True) as c:
        c.execute((ROOT / "db" / "sql" / "0002_observations.down.sql").read_text(encoding="utf-8"))
        names = {r[0] for r in c.execute("SELECT relname FROM pg_class WHERE relnamespace = 'geonaf'::regnamespace "
                                         "AND relkind IN ('r', 'v')").fetchall()}
        assert names == {"geometries"}
        assert c.execute("SELECT to_regprocedure('geonaf.forbid_change()') IS NOT NULL").fetchone()[0]
