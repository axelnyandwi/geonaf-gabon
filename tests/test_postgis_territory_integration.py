"""PostGIS territory registry against a real database (marker ``postgis``); skipped without one.

Requires ``GEONAF_TEST_DATABASE_URL`` (``postgresql+psycopg://geonaf_migrator@127.0.0.1:5433/geonaf_test``),
``requirements-db.txt`` and the PostGIS extension created by an administrator. The database name must
end with ``_test``: each test drops and recreates the ``geonaf`` schema there. See
docs/postgis_registry.md. Nothing is written to the file registry or to the demo pack.
"""

import os
from pathlib import Path

import pytest
from shapely.geometry import box

from src.data import postgis_territory_registry as pg
from src.data.territory_registry import (
    ACQUISITION_ZONE, ADMIN_UNIT, COMMUNITY, OFFICIAL_VERIFIED, TerritoryRegistry, TerritorySource,
)
from tests.test_aoi_identity import write_geojson
from tests.test_postgis_territory_unit import (  # noqa: F401  (registry is a fixture)
    DEPARTMENT, NOW, assert_parity, real_registry_dir, registry, snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
TEST_VARIABLE = "GEONAF_TEST_DATABASE_URL"
TEST_URL = os.environ.get(TEST_VARIABLE, "").strip()
pytestmark = [pytest.mark.postgis,
              pytest.mark.skipif(not TEST_URL, reason=f"{TEST_VARIABLE} non défini : tests PostGIS non exécutés.")]


def connect():
    import psycopg

    return psycopg.connect(pg.psycopg_conninfo(TEST_URL))


@pytest.fixture
def database():
    """Empty geonaf schema created by migration 0001, on a database whose name ends with _test."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    if not str(conninfo_to_dict(pg.psycopg_conninfo(TEST_URL)).get("dbname", "")).endswith("_test"):
        pytest.fail("La base de test doit se terminer par « _test » (le schéma geonaf y est supprimé).")
    with psycopg.connect(pg.psycopg_conninfo(TEST_URL), autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        connection.execute((ROOT / "db" / "sql" / "0001_territorial_registry.up.sql").read_text(encoding="utf-8"))
    yield connect
    with psycopg.connect(pg.psycopg_conninfo(TEST_URL), autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")


def scalar(sql, params=()):
    with connect() as connection:
        return connection.execute(sql, params).fetchone()[0]


def run_import(registry, apply=True):
    with connect() as connection:
        return pg.apply_import(connection, pg.plan_import(registry), source_label="registre de test",
                               code_version="test", apply=apply)


def test_alembic_upgrade_downgrade_upgrade(monkeypatch):
    pytest.importorskip("psycopg")
    pytest.importorskip("alembic")
    from alembic import command
    from alembic.config import Config

    monkeypatch.setenv(pg.URL_VARIABLE, TEST_URL)
    with connect() as connection:
        connection.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        connection.execute("DROP TABLE IF EXISTS public.alembic_version")
        connection.commit()
    config = Config(str(ROOT / "db" / "alembic.ini"))
    try:
        command.upgrade(config, "head")
        assert scalar("SELECT count(*) FROM geonaf.licences") == 3
        command.downgrade(config, "base")
        assert scalar("SELECT count(*) FROM pg_namespace WHERE nspname = 'geonaf'") == 0
        command.upgrade(config, "head")
        assert scalar("SELECT version_num FROM public.alembic_version") == "0001_territorial_registry"
    finally:
        command.downgrade(config, "base")


def test_dry_run_writes_nothing_and_import_is_idempotent(database, registry):
    before = snapshot(registry.data_dir)
    dry = run_import(registry, apply=False)
    assert (dry.applied, dry.inserted, dry.existing, dry.import_id) == (False, 6, 0, None)
    assert scalar("SELECT count(*) FROM geonaf.territory_versions") == 0
    assert scalar("SELECT count(*) FROM geonaf.registry_imports") == 0
    first = run_import(registry)
    assert (first.inserted, first.existing) == (6, 0) and first.import_id is not None
    second = run_import(registry)
    assert (second.inserted, second.existing) == (0, 6)
    assert scalar("SELECT count(*) FROM geonaf.territory_versions") == 6
    assert scalar("SELECT count(*) FROM geonaf.geometries") == 3
    assert scalar("SELECT count(*) FROM geonaf.source_releases") == 3
    assert scalar("SELECT count(*) FROM geonaf.registry_imports WHERE finished_at IS NOT NULL") == 2
    assert scalar("SELECT fingerprints_sha256 FROM geonaf.registry_imports ORDER BY import_id LIMIT 1") == \
        first.fingerprints_sha256
    assert snapshot(registry.data_dir) == before                          # File registry untouched.


def test_repository_reads_exactly_what_the_file_registry_reads(database, registry):
    run_import(registry)
    repository = pg.PostgisTerritoryRegistry(connect, registry.data_dir)
    assert_parity(registry, repository)
    for bounds in ((9.39, 0.39, 9.41, 0.41), (9.0, 0.0, 10.0, 1.0), (9.31, 0.31, 9.33, 0.33), (0, 0, 1, 1)):
        expected = [r.territory_id for r in registry.territories()
                    if pg.wgs84_union(registry.geometry_path(r)).intersects(box(*bounds))]
        assert [r.territory_id for r in repository.intersecting(bounds)] == expected
    assert scalar("SELECT count(*) FROM geonaf.geometries WHERE GeometryType(geom) = 'MULTIPOLYGON'") == 3
    assert scalar("SELECT count(*) FROM geonaf.geometries WHERE source_geometry_type = 'Polygon'") == 2


@pytest.mark.skipif(real_registry_dir() is None, reason="Registre du paquet de démonstration absent.")
def test_real_registry_import_and_parity(database):
    files = TerritoryRegistry(real_registry_dir())
    before = snapshot(real_registry_dir() / "territories")
    report = run_import(files)
    assert (report.inserted, report.geometries, report.releases) == (97, 97, 2)
    assert run_import(files).inserted == 0
    assert_parity(files, pg.PostgisTerritoryRegistry(connect, real_registry_dir()))
    akanda = (9.38, 0.50, 9.50, 0.62)
    expected = [r.territory_id for r in files.territories()
                if pg.wgs84_union(files.geometry_path(r)).intersects(box(*akanda))]
    assert [r.territory_id for r in pg.PostgisTerritoryRegistry(connect, real_registry_dir()).intersecting(akanda)] \
        == expected and expected
    assert snapshot(real_registry_dir() / "territories") == before


def test_divergent_content_is_refused_and_the_database_is_unchanged(database, registry, tmp_path):
    run_import(registry)
    other = TerritoryRegistry(tmp_path / "other")
    square = write_geojson(tmp_path / "sq.geojson", [box(9.40, 0.40, 9.45, 0.45)])
    other.register(territory_id="gab-gbopen-adm2-a", label="Autre libellé", kind=ADMIN_UNIT, legal_status=COMMUNITY,
                   source=registry.get("gab-gbopen-adm2-a").source, geometry_path=square, admin=DEPARTMENT, now=NOW)
    with pytest.raises(pg.TerritoryImportError, match="Divergence"):
        run_import(other)
    assert scalar("SELECT count(*) FROM geonaf.territory_versions") == 6
    assert scalar("SELECT count(*) FROM geonaf.registry_imports") == 1


def test_missing_licence_is_refused(database, tmp_path):
    other = TerritoryRegistry(tmp_path / "other")
    square = write_geojson(tmp_path / "sq.geojson", [box(9.40, 0.40, 9.45, 0.45)])
    other.register(territory_id="osm-zone", label="Zone", kind=ADMIN_UNIT, legal_status=COMMUNITY,
                   source=TerritorySource("osm", "OSM", "2026", "ODbL-1.0", "© OpenStreetMap contributors"),
                   geometry_path=square, admin=DEPARTMENT, now=NOW)
    with pytest.raises(pg.TerritoryImportError, match="ODbL-1.0"):
        run_import(other)
    assert scalar("SELECT count(*) FROM geonaf.registry_imports") == 0


def insert_version(row):
    from psycopg.types.json import Jsonb

    """Insert a crafted row; the document keys checked by the table follow the row, so only the rule under
    test can refuse it."""
    keys = ("territory_id", "version", "geometry_sha256", "file_sha256")
    row = dict(row, record_document=Jsonb(dict(row["record_document"], **{k: row[k] for k in keys})))
    columns = list(row)
    values = ", ".join(f"%({c})s::float8[]" if c == "wgs84_bounds" else f"%({c})s::text[]" if c == "limits"
                       else f"%({c})s" for c in columns)
    with connect() as connection:
        connection.execute(f"INSERT INTO geonaf.territory_versions ({', '.join(columns)}) VALUES ({values})", row)


def test_constraints_refuse_what_the_file_registry_refuses(database, registry):
    import psycopg

    run_import(registry)
    base = pg.version_row(registry.get("gab-gbopen-adm2-a"))
    base.update(release_id=scalar("SELECT release_id FROM geonaf.source_releases WHERE source_id = 'geoboundaries-gbopen'"),
                import_id=scalar("SELECT import_id FROM geonaf.registry_imports LIMIT 1"))
    zone = pg.version_row(registry.get("akanda", 1))
    zone.update(release_id=scalar("SELECT release_id FROM geonaf.source_releases WHERE source_id = 'geonaf'"),
                import_id=base["import_id"])
    fake = "f" * 64
    invalid = [
        dict(base, version=2, supersedes=base["record_fingerprint"], record_fingerprint=fake,
             legal_status=OFFICIAL_VERIFIED, legal_evidence=None),               # Official without evidence.
        dict(zone, version=3, supersedes="e" * 64, record_fingerprint=fake),     # Wrong predecessor.
        dict(zone, version=3, supersedes=scalar("SELECT record_fingerprint FROM geonaf.territory_versions WHERE "
                                                "territory_id = 'akanda' AND version = 2"),
             record_fingerprint=fake, label="Commune d'Akanda"),                 # Administrative word.
        dict(base, version=5, supersedes=base["record_fingerprint"], record_fingerprint=fake),   # Version gap.
        dict(base, version=2, supersedes=base["record_fingerprint"], record_fingerprint=fake,
             storage_key="C:/data/x.geojson"),                                   # Absolute key.
        dict(base, version=2, supersedes=base["record_fingerprint"], record_fingerprint=fake,
             legal_status="zone d'acquisition GEONAF"),                          # Status of another kind.
        dict(base, version=2, supersedes=base["record_fingerprint"], record_fingerprint=fake, parts=2),
        dict(base, version=2, supersedes=base["record_fingerprint"], record_fingerprint=fake,
             kind=ACQUISITION_ZONE),                                             # A territory never changes kind.
    ]
    for row in invalid:
        with pytest.raises(psycopg.Error):
            insert_version(row)
    with pytest.raises(psycopg.Error), connect() as connection:     # Self-intersecting ring.
        connection.execute("INSERT INTO geonaf.geometries VALUES (%s, 'geonaf-aoi-geometry-v1', 'Polygon', "
                           "ST_GeomFromText('MULTIPOLYGON(((0 0,1 1,1 0,0 1,0 0)))', 4326), 1, 5)", ("a" * 64,))
    assert scalar("SELECT count(*) FROM geonaf.territory_versions") == 6
    insert_version(dict(base, version=2, supersedes=base["record_fingerprint"], record_fingerprint="d" * 64))
    assert scalar("SELECT count(*) FROM geonaf.territory_versions") == 7   # Control: a valid row is accepted.


def test_rows_are_immutable(database, registry):
    import psycopg

    run_import(registry)
    statements = ["UPDATE geonaf.territory_versions SET label = 'x'", "DELETE FROM geonaf.geometries",
                  "TRUNCATE geonaf.licences CASCADE", "UPDATE geonaf.licences SET name = 'x'",
                  "DELETE FROM geonaf.registry_imports",
                  "UPDATE geonaf.registry_imports SET finished_at = now()"]   # Already closed: not reopenable.
    for statement in statements:
        with pytest.raises(psycopg.Error), connect() as connection:
            connection.execute(statement)
    assert scalar("SELECT count(*) FROM geonaf.territory_versions") == 6


def test_role_privileges_when_roles_exist(database):
    if scalar("SELECT count(*) FROM pg_roles WHERE rolname IN ('geonaf_reader', 'geonaf_importer')") < 2:
        pytest.skip("Rôles geonaf_reader et geonaf_importer absents (db/sql/roles.sql).")
    checks = {("geonaf_reader", "geonaf.territory_versions", "SELECT"): True,
              ("geonaf_reader", "geonaf.territory_versions", "INSERT"): False,
              ("geonaf_importer", "geonaf.territory_versions", "INSERT"): True,
              ("geonaf_importer", "geonaf.territory_versions", "UPDATE"): False,
              ("geonaf_importer", "geonaf.licences", "INSERT"): False}
    for (role, table, privilege), allowed in checks.items():
        assert scalar("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)) is allowed
