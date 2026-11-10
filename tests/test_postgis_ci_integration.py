"""Mandatory synthetic CI checks, using the same destructive-test capability."""
from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from tests.postgis_support import configure_alembic, isolated_connection
from tests.test_postgis_storage_integration import (
    ROOT, TEST_URL, acquisitions, connect, local, migrated, registry,
)
from tests.test_observations import make_indicator
from src.data import observations as obs
from src.data import postgis_observations as pgo
from src.data import postgis_territory_registry as pg
from src.data.database import DatabaseConfigError, EnvironmentIdentityError

pytestmark = [pytest.mark.postgis, pytest.mark.skipif(not TEST_URL, reason="Isolated PostGIS required")]


def test_migration_failure_rolls_back_all_ddl(local, monkeypatch):
    from alembic import command
    from alembic.config import Config

    configure_alembic(monkeypatch, TEST_URL)
    with connect(autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS public.alembic_version")
    original = Path.read_text

    def broken(path, *args, **kwargs):
        body = original(path, *args, **kwargs)
        return body + "\nSELECT 1/0;" if path.name == "0002_observations.up.sql" else body

    config = Config(str(ROOT / "db/alembic.ini"))
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", broken)
        with pytest.raises(Exception):
            command.upgrade(config, "head")
    with connect() as c:
        assert c.execute("SELECT to_regnamespace('geonaf')").fetchone()[0] is None
        assert c.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0] is None
    command.upgrade(config, "head")
    with connect(autocommit=True) as c:
        assert c.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] == "0002_observations"
        c.execute("DROP TABLE public.alembic_version")


def test_multi_scope_failure_is_atomic(migrated, acquisitions, registry, tmp_path):
    from scripts.import_observations_postgis import run_imports
    from src.geo.aoi_identity import load_aoi_identity

    records = obs.FileAcquisitionCatalog(acquisitions).scan().records
    zone = registry.geometry_path(registry.territories()[0])
    result = obs.IndicatorResultStore(tmp_path / "results").save(
        make_indicator(load_aoi_identity(zone).file_sha256), zone, code_version="ci",
        computed_at=datetime(2026, 11, 5, tzinfo=timezone.utc))
    batches = {"acquisitions": records, "indicators": [result]}
    importers = {"acquisitions": pgo.apply_acquisition_import, "indicators": pgo.apply_indicator_import}
    with connect() as c:
        with pytest.raises(pgo.ObservationImportError):
            run_imports(c, batches, importers, source_label="synthetic", code_version="ci", environment="test", apply=True)
    with connect() as c:
        for table in ("acquisitions", "acquisition_manifests", "data_imports", "indicator_results"):
            assert c.execute("SELECT count(*) FROM geonaf." + table).fetchone()[0] == 0
        pg.apply_import(c, pg.plan_import(registry), source_label="synthetic", code_version="ci", expected_environment="test", apply=True)
    for expected in ((2, 1), (0, 0)):
        with connect() as c:
            reports = run_imports(c, batches, importers, source_label="synthetic", code_version="ci", environment="test", apply=True)
            assert tuple(report.inserted for _, report in reports) == expected


def test_effective_reader_rls_and_importer(migrated, registry):
    import psycopg

    # Independent login connections: SET ROLE under a superuser would not prove this.
    with isolated_connection(os.environ["GEONAF_CI_IMPORTER_URL"]) as c:
        assert c.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone() == (False, False)
        pg.apply_import(c, pg.plan_import(registry), source_label="synthetic", code_version="ci", expected_environment="test", apply=True)
    with isolated_connection(os.environ["GEONAF_CI_READER_URL"]) as c:
        assert c.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone() == (False, False)
        assert c.execute("SELECT count(*) FROM geonaf.territory_versions").fetchone()[0] == 6
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("DELETE FROM geonaf.territory_versions")
    with connect() as c:
        c.execute("DROP POLICY geonaf_reader_select ON geonaf.territory_versions")
    try:
        with isolated_connection(os.environ["GEONAF_CI_READER_URL"]) as c:
            assert c.execute("SELECT has_table_privilege(current_user,'geonaf.territory_versions','SELECT')").fetchone()[0]
            assert c.execute("SELECT count(*) FROM geonaf.territory_versions").fetchone()[0] == 0
    finally:
        with connect() as c:
            c.execute("CREATE POLICY geonaf_reader_select ON geonaf.territory_versions FOR SELECT TO geonaf_reader USING (true)")


def test_wrong_identity_and_unauthorized_drop_are_refused(migrated, monkeypatch):
    import uuid
    from alembic import command

    with monkeypatch.context() as patch:
        patch.setenv("GEONAF_TEST_INSTANCE_ID", str(uuid.uuid4()))
        with pytest.raises((DatabaseConfigError, EnvironmentIdentityError)):
            with connect() as c:
                c.execute("DROP SCHEMA geonaf CASCADE")
    with monkeypatch.context() as patch:
        patch.setenv("GEONAF_ALLOW_DESTRUCTIVE_TESTS", "0")
        with pytest.raises(DatabaseConfigError):
            with connect() as c:
                c.execute("DROP SCHEMA geonaf CASCADE")
    with connect() as c:
        c.execute("COMMENT ON SCHEMA geonaf IS 'geonaf-environment:dev'")
    try:
        with pytest.raises(Exception):
            command.downgrade(migrated, "base")
        with connect() as c:
            assert c.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] == "0002_observations"
    finally:
        with connect() as c:
            c.execute("COMMENT ON SCHEMA geonaf IS 'geonaf-environment:test'")


def test_real_gist_catalog_and_plans(migrated, registry, acquisitions):
    with connect() as c:
        pg.apply_import(c, pg.plan_import(registry), source_label="synthetic", code_version="ci", expected_environment="test", apply=True)
        pgo.apply_acquisition_import(c, obs.FileAcquisitionCatalog(acquisitions).scan().records,
                                    source_label="synthetic", code_version="ci", expected_environment="test", apply=True)
        c.execute("SET LOCAL enable_seqscan = off")
        for name, table, column in (("geometries_geom_gist", "geometries", "geom"),
                                    ("acquisition_manifests_footprint", "acquisition_manifests", "footprint")):
            row = c.execute("SELECT am.amname, i.indisvalid FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid JOIN pg_am am ON am.oid=idx.relam WHERE idx.oid=to_regclass(%s)", ("geonaf." + name,)).fetchone()
            assert row == ("gist", True)
            plan = c.execute("EXPLAIN (FORMAT JSON) SELECT * FROM geonaf." + table + " WHERE " + column + " && ST_MakeEnvelope(9,0,10,1,4326)").fetchone()[0]

            def indexes(value):
                if isinstance(value, dict):
                    return [value.get("Index Name")] + [v for child in value.values() for v in indexes(child)]
                if isinstance(value, list):
                    return [v for child in value for v in indexes(child)]
                return []

            assert name in indexes(plan)
