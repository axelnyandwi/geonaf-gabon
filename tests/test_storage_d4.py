"""Storage selection (D4), guarded PostGIS sessions (R5 bis) and the territory catalogue in both modes; offline.

The database is replaced by fakes that record the statements, and by the in-memory emulation of the
registry's queries (tests/test_postgis_territory_unit.py). The real database is exercised by
tests/test_postgis_storage_integration.py (marker ``postgis``).
"""

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from src.data import storage
from src.data.database import DatabaseConfigError, DatabaseConnectionError, EnvironmentIdentityError
from src.data.postgis_session import (
    POSTGIS_SCHEMA_QUERY, PostgisSessions, PostgisUnavailableError, plain_read_transaction, set_postgis_search_path,
)
from src.data.database import parse_target
from src.services import territory_catalog as catalog
from tests.test_postgis_territory_unit import OfflineRegistry, registry  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "postgresql+psycopg://geonaf_reader:s3cret-Pa55@aws-0-eu.pooler.supabase.com:6543/postgres"


@pytest.fixture(scope="module")
def ca_file(tmp_path_factory):
    """Synthetic PEM CA; no remote TLS handshake is performed."""
    trustme = pytest.importorskip("trustme")
    path = tmp_path_factory.mktemp("ca") / "geonaf-test-ca.pem"
    trustme.CA().cert_pem.write_to_path(path)
    return str(path)


@pytest.fixture
def remote(ca_file):
    return parse_target(REMOTE, "dev", sslrootcert=ca_file)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        if sql == POSTGIS_SCHEMA_QUERY:
            self._row = (self.connection.postgis_schema,) if self.connection.postgis_schema else None
        elif "obj_description" in sql:
            self._row = (self.connection.mark,)
        else:
            self._row = ("ok",)

    def fetchone(self):
        return self._row


class FakeConnection:
    def __init__(self, *, postgis_schema="extensions", mark="geonaf-environment:dev"):
        self.postgis_schema, self.mark = postgis_schema, mark
        self.executed, self.read_only, self.rolled_back, self.closed = [], None, False, False

    def cursor(self, **_):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


# --- R5 bis: search path local to the transaction ---------------------------------------------------


def test_search_path_is_built_from_the_postgis_extension_schema_and_never_searches_public():
    connection = FakeConnection(postgis_schema="extensions")
    with connection.cursor() as cursor:
        assert set_postgis_search_path(cursor) == "extensions"
    (lookup, _), (setting, params) = connection.executed
    assert lookup == POSTGIS_SCHEMA_QUERY and "pg_extension" in lookup
    assert params == ("extensions",)                               # Bound value, quoted by format('%I').
    assert "'pg_catalog, %%I, pg_temp'" in setting and ", true)" in setting   # Local to the transaction.
    assert "public" not in setting


def test_missing_postgis_is_reported_and_no_path_is_set():
    connection = FakeConnection(postgis_schema=None)
    with connection.cursor() as cursor, pytest.raises(PostgisUnavailableError):
        set_postgis_search_path(cursor)
    assert len(connection.executed) == 1


def test_application_session_is_read_only_timed_marked_and_always_rolled_back(remote):
    connection = FakeConnection()
    sessions = PostgisSessions(remote, connect=lambda: connection)
    with sessions.read() as session:
        assert session is connection and connection.read_only is True
    statements = [sql for sql, _ in connection.executed]
    assert "statement_timeout" in statements[0]                     # First: bounded duration.
    assert "obj_description" in statements[1]                       # Then the environment mark...
    assert statements[2] == POSTGIS_SCHEMA_QUERY                    # ...then the R5 bis path.
    assert connection.rolled_back and connection.closed


def test_application_session_refuses_another_environment_before_handing_out_the_connection(remote):
    connection = FakeConnection(mark="geonaf-environment:test")
    sessions = PostgisSessions(remote, connect=lambda: connection)
    with pytest.raises(EnvironmentIdentityError):
        with sessions.read():
            pytest.fail("connexion remise malgré une marque différente")
    assert connection.rolled_back and connection.closed


def test_session_is_closed_when_the_query_fails(remote):
    connection = FakeConnection()
    sessions = PostgisSessions(remote, connect=lambda: connection)
    with pytest.raises(KeyError), sessions.read():
        raise KeyError("échec de la requête")
    assert connection.rolled_back and connection.closed


def test_connection_error_message_names_no_host_user_or_password(remote):
    psycopg = pytest.importorskip("psycopg")

    def refuse():
        raise psycopg.OperationalError('connection to server at "aws-0-eu.pooler.supabase.com" failed: '
                                       'password authentication failed for user "geonaf_reader" s3cret-Pa55')
    sessions = PostgisSessions(remote, connect=refuse)
    with pytest.raises(DatabaseConnectionError) as caught:
        with sessions.read():
            pass
    message = str(caught.value)
    assert message == "Base PostGIS indisponible (OperationalError). Réessayer plus tard."
    for secret in ("supabase", "geonaf_reader", "s3cret", "6543"):
        assert secret not in message


def test_plain_transaction_sets_the_path_without_a_mark_and_rolls_back():
    connection = FakeConnection(mark=None)
    with plain_read_transaction(lambda: connection):
        pass
    assert [sql for sql, _ in connection.executed][0] == POSTGIS_SCHEMA_QUERY
    assert connection.read_only is True and connection.rolled_back and connection.closed


# --- Storage selection ----------------------------------------------------------------------------


def test_file_mode_is_the_default_and_keeps_the_current_readers(tmp_path):
    store = storage.open_store(tmp_path, environ={})
    assert store.mode == storage.FILE and store.writable and not store.is_postgis
    assert type(store.territories).__name__ == "TerritoryRegistry"
    assert type(store.acquisitions).__name__ == "FileAcquisitionCatalog"
    assert type(store.indicators).__name__ == "IndicatorResultStore"


@pytest.mark.parametrize("environ, fragment", [
    ({"GEONAF_DB_BACKEND": "sqlite"}, "GEONAF_DB_BACKEND"),
    ({"GEONAF_DB_BACKEND": "postgres"}, "GEONAF_DB_ENV"),
    ({"GEONAF_DB_BACKEND": "postgres", "GEONAF_DB_ENV": "prod", "GEONAF_DATABASE_URL": REMOTE}, "GEONAF_DB_ENV"),
    ({"GEONAF_DB_BACKEND": "postgres", "GEONAF_DB_ENV": "dev",
      "GEONAF_DATABASE_URL": REMOTE + "?sslmode=disable"}, "verify-full"),
    ({"GEONAF_DB_BACKEND": "postgres", "GEONAF_DB_ENV": "dev", "GEONAF_DATABASE_URL": REMOTE}, "GEONAF_DB_SSLROOTCERT"),
])
def test_postgis_mode_refuses_an_incomplete_or_unsafe_configuration(tmp_path, environ, fragment):
    with pytest.raises(DatabaseConfigError) as caught:
        storage.open_store(tmp_path, environ=environ)
    assert fragment in str(caught.value) and "s3cret" not in str(caught.value)


def test_postgis_store_opens_without_connecting_and_exposes_only_the_environment(tmp_path, ca_file):
    calls = []
    environ = {"GEONAF_DB_BACKEND": "postgres", "GEONAF_DB_ENV": "test", "GEONAF_DATABASE_URL": REMOTE,
               "GEONAF_DB_SSLROOTCERT": ca_file}
    store = storage.postgis_store(tmp_path, environ, connect=lambda: calls.append(1))
    assert calls == [] and store.is_postgis and not store.writable and store.environment == "test"
    assert "s3cret" not in repr(store) and "s3cret" not in store.description
    assert type(store.territories).__name__ == "PostgisTerritoryRegistry"


def test_file_mode_never_loads_a_database_driver():
    code = ("import sys, src.data.storage as s; s.open_store('.', environ={}); "
            "import src.components.data_source, src.components.territory_explorer; "
            "print(sorted(m for m in ('psycopg', 'sqlalchemy', 'alembic') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "[]"


# --- Territory catalogue in both modes ----------------------------------------------------------------


def _postgis_like(registry_fixture):
    from src.data import postgis_territory_registry as pg

    return OfflineRegistry(pg.plan_import(registry_fixture), registry_fixture.data_dir)


def test_catalogue_offers_the_same_territories_and_geometries_from_postgis(registry):  # noqa: F811
    database = _postgis_like(registry)
    for release in ("gbOpen", "gbHumanitarian"):
        files = catalog.admin_territories(registry.data_dir, release, "ADM2", registry=registry)
        rows = catalog.admin_territories(registry.data_dir, release, "ADM2", registry=database)
        assert [(o.key, o.label, o.legal_status, o.area_km2) for o in files] == \
               [(o.key, o.label, o.legal_status, o.area_km2) for o in rows]
        for file_option, row_option in zip(files, rows):
            assert row_option.geometry_path is None                          # No file needed in PostGIS mode.
            assert row_option.file_sha256 == file_option.file_sha256 == \
                __import__("hashlib").sha256(file_option.geometry_path.read_bytes()).hexdigest()   # Same cache keys.
            assert catalog.geometry_wgs84(row_option).equals_exact(catalog.geometry_wgs84(file_option), 0)
            assert catalog.selection_payload(row_option) == catalog.selection_payload(file_option)


def test_catalogue_uses_the_store_selected_by_the_environment(registry, monkeypatch):  # noqa: F811
    monkeypatch.delenv("GEONAF_DB_BACKEND", raising=False)
    options = catalog.custom_zones(registry.data_dir)
    assert all(o.registry is not None and o.geometry_path.is_file() for o in options)


def test_a_geometry_that_no_longer_matches_its_record_is_refused_in_both_modes(registry):  # noqa: F811
    import shapely
    from shapely.geometry import box

    from src.data import postgis_territory_registry as pg

    record = registry.territories()[0]
    database = _postgis_like(registry)
    forged = shapely.geometry.MultiPolygon([box(0, 0, 1, 1)])                 # Valid stored form, other area.
    database.plan.geometries[record.geometry_sha256] = pg.GeometryRow(
        record.geometry_sha256, "Polygon", shapely.to_wkb(forged, byte_order=1), 1, 5)
    option = catalog.TerritoryOption("territory:x", record.territory_id, record.label, record.kind,
                                     record.legal_status, None, record.area_km2, record, database)
    with pytest.raises(ValueError, match="empreinte"):
        catalog.geometry_wgs84(option)                                      # PostGIS row edited.
    path = registry.store.path(record.storage_key)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b" ")                             # One byte more: other digest.
    with pytest.raises(ValueError, match="modifiée"):
        registry.geometry(record)                                           # File edited: digest refused.


def test_file_registry_intersecting_matches_the_postgis_query(registry):  # noqa: F811
    database = _postgis_like(registry)
    for bounds in ((9.39, 0.39, 9.41, 0.41), (9.33, 0.29, 9.35, 0.33), (0, 0, 1, 1)):
        assert [r.territory_id for r in registry.intersecting(bounds)] == \
               [r.territory_id for r in database.intersecting(bounds)]


def test_territory_comparison_reads_geometries_through_the_registry(registry):  # noqa: F811
    from src.services.territory_comparison import compare_territories

    database = _postgis_like(registry)
    a, b = "gab-gbopen-adm2-a", "gab-gbhumanitarian-adm2-a"
    assert compare_territories(registry, a, b) == compare_territories(database, a, b)


def test_compare_options_refuses_options_from_two_storages(registry):  # noqa: F811
    database = _postgis_like(registry)
    files = {o.label: o for o in catalog.admin_territories(registry.data_dir, "gbOpen", "ADM2", registry=registry)}
    rows = {o.label: o for o in catalog.admin_territories(registry.data_dir, "gbHumanitarian", "ADM2",
                                                          registry=database)}
    with pytest.raises(ValueError, match="même stockage"):
        catalog.compare_options(files["Unité A"], rows["Unité A"])


# --- Interface ------------------------------------------------------------------------------------


def test_comparison_tab_in_postgis_mode_never_reads_the_file_registry(registry, tmp_path, monkeypatch):  # noqa: F811
    """D2-D4 audit: the tab used to compute areas from data/territories whatever the selected storage."""
    from streamlit.testing.v1 import AppTest

    import src.config
    from src.data import territory_registry as files_module
    from src.services.territory_comparison import compare_territories

    expected = compare_territories(registry, "gab-gbopen-adm2-a", "gab-gbhumanitarian-adm2-a")
    database = _postgis_like(registry)
    reads = []
    original_rows = database._rows
    database._rows = lambda query, params: reads.append(query) or original_rows(query, params)
    empty = tmp_path / "donnees_vides"                       # No territories/ folder: PostGIS only.
    empty.mkdir()
    monkeypatch.setattr(src.config, "DATA_DIR", empty)
    fake = storage.DataStore(storage.POSTGRES, "masqué", database, None, None, "test")
    monkeypatch.setattr(storage, "open_store", lambda data_dir, environ=None: fake)

    def forbidden(*_, **__):
        raise AssertionError("registre fichier lu en mode PostGIS")
    monkeypatch.setattr(files_module.TerritoryRegistry, "versions", forbidden)
    monkeypatch.setattr(files_module.TerritoryRegistry, "geometry_path", forbidden)
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.run(timeout=60)
    reads.clear()
    app = app.switch_page("views/territoires.py").run(timeout=60)
    assert not app.exception and not app.error
    metrics = {m.label: m.value for m in app.metric}
    from src.components.formatting import format_percent, format_surface

    assert metrics["gbOpen (km²)"] == format_surface(expected.area_a_km2)
    assert metrics["gbHumanitarian (km²)"] == format_surface(expected.area_b_km2)
    assert metrics["Recouvrement (%)"] == format_percent(expected.overlap_percent)
    # Measured reads on the first rerun of the page (one connection each): 2 lists (read once, reused for the
    # pairs instead of read again), 2 versions, 2 geometries for the metrics, 2 for the comparison map, which
    # is then served by its cache.
    assert reads == ["latest", "latest", "versions", "versions", "geometry", "geometry", "geometry", "geometry"]


def test_postgis_mode_disables_every_write_of_the_territory_page(registry, monkeypatch):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", registry.data_dir)
    database = _postgis_like(registry)
    fake = storage.DataStore(storage.POSTGRES, "masqué", database, None, None, "test")
    monkeypatch.setattr(storage, "open_store", lambda data_dir, environ=None: fake)
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.run(timeout=60)
    app = app.switch_page("views/territoires.py").run(timeout=60)
    assert not app.exception
    shown = [e.value for kind in (app.info, app.caption, app.warning) for e in kind]
    assert any("Stockage : base PostGIS" in t and "« test »" in t for t in shown)
    assert any("L'import d'une zone personnalisée" in t for t in shown)
    assert not app.get("file_uploader")                                   # Nothing can be received.
    assert "masqué" not in " ".join(shown)


# --- Stored acquisitions and results of one zone (Streamlit priorities 3 and 4) ---------------------------


def test_stored_observations_show_intersecting_acquisitions_and_results_of_the_exact_geometry(tmp_path):
    import hashlib

    import geopandas as gpd
    from shapely.geometry import box

    from tests.test_observations import make_indicator, write_product

    data = tmp_path / "data"
    write_product(data, "S2B_LATER", "2025-01-25T09:31:59.024Z",
                  datetime(2025, 1, 26, 12, tzinfo=timezone.utc))              # Grid near lon 9.0, lat 0.904.
    for name, bounds in (("near", (8.999, 0.90, 9.001, 0.91)), ("far", (10.0, 1.0, 10.1, 1.1))):
        path = tmp_path / f"{name}.geojson"
        gpd.GeoDataFrame(geometry=[box(*bounds)], crs=4326).to_file(path)
        catalog.import_custom_zone(data, path.read_bytes(), label=f"Zone {name}", author="GEONAF",
                                   license_spdx="CC-BY-4.0", source_date="2026-11-05")
    store = storage.file_store(data)
    options = {o.label: o for o in catalog.custom_zones(data)}
    near, far = options["Zone near"], options["Zone far"]
    for option in (near, far):
        zone = store.territories.geometry_path(option.record)
        store.indicators.save(make_indicator(hashlib.sha256(zone.read_bytes()).hexdigest()), zone, code_version="t")
    found = catalog.stored_observations(near, store)
    assert [a.product_id for a in found.acquisitions] == ["S2B_LATER"]
    assert [r.geometry_sha256 for r in found.results] == [near.record.geometry_sha256]    # Never the other zone's.
    assert catalog.stored_observations(far, store).acquisitions == ()


def test_territory_page_shows_the_stored_panel_on_demand_only(tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box
    from streamlit.testing.v1 import AppTest

    import src.config

    (tmp_path / "aoi").mkdir()
    gpd.GeoDataFrame(geometry=[box(9.40, 0.40, 9.50, 0.50)], crs=4326).to_file(tmp_path / "aoi" / "akanda.geojson")
    monkeypatch.setattr(src.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    calls = []
    original = catalog.stored_observations
    monkeypatch.setattr(catalog, "stored_observations", lambda *a: calls.append(1) or original(*a))
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.run(timeout=60)
    app = app.switch_page("views/territoires.py").run(timeout=60)
    assert not app.exception and calls == []                                    # Nothing read until asked.
    assert not any("Stockage :" in c.value for c in app.caption)                  # File mode: no new caption.
    app.toggle(key="stored_aoi:akanda").set_value(True).run(timeout=60)
    assert not app.exception and calls == [1]
    shown = [e.value for e in app.info]
    assert any(t.startswith("Aucune acquisition à manifeste d'ingestion strict") and "subset_manifest.json" in t
               for t in shown)
    assert "Aucun résultat d'indicateur enregistré pour cette géométrie exacte." in shown


def test_stored_results_are_shown_with_their_scientific_status(tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box
    from streamlit.testing.v1 import AppTest

    import src.config
    from src.data.observations import IndicatorResult
    from tests.test_observations import make_indicator

    (tmp_path / "aoi").mkdir()
    gpd.GeoDataFrame(geometry=[box(9.40, 0.40, 9.50, 0.50)], crs=4326).to_file(tmp_path / "aoi" / "akanda.geojson")
    monkeypatch.setattr(src.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    result = IndicatorResult("r" * 64, "d" * 64, "g" * 64, "2026-11-05T00:00:00+00:00", "t", make_indicator("a" * 64), {})
    monkeypatch.setattr(catalog, "stored_observations",
                        lambda option, store: catalog.StoredObservations("g" * 64, (), (result,)))
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.run(timeout=60)
    app = app.switch_page("views/territoires.py").run(timeout=60)
    app.toggle(key="stored_aoi:akanda").set_value(True).run(timeout=60)
    assert not app.exception
    table = app.dataframe[-1].value
    assert table.iloc[0]["Statut scientifique"] == "expérimental (méthode non validée)"
    assert table.iloc[0]["Valeur"] == "12.5 km²" and table.iloc[0]["Calculé le (UTC)"] == "2026-11-05 00:00"


def test_driver_errors_are_named_by_kind_never_by_their_text():
    psycopg = pytest.importorskip("psycopg")
    from src.data.postgis_session import driver_error_message

    secret = 'connection to "aws-0-eu.pooler.supabase.com" user "geonaf_reader" s3cret-Pa55'
    denied = psycopg.errors.InsufficientPrivilege(secret)
    cancelled = psycopg.errors.QueryCanceled(secret)
    other = psycopg.OperationalError(secret)
    assert driver_error_message(denied).startswith("Droits insuffisants")
    assert driver_error_message(cancelled).startswith("Délai maximal de requête dépassé")
    assert driver_error_message(other).startswith("Base PostGIS indisponible")
    for error in (denied, cancelled, other):
        message = driver_error_message(error)
        assert not any(part in message for part in ("supabase", "geonaf_reader", "s3cret"))


def test_store_repr_holds_neither_host_nor_user(tmp_path, ca_file):
    environ = {"GEONAF_DB_BACKEND": "postgres", "GEONAF_DB_ENV": "test", "GEONAF_DATABASE_URL": REMOTE,
               "GEONAF_DB_SSLROOTCERT": ca_file}
    store = storage.postgis_store(tmp_path, environ, connect=lambda: None)
    assert "pooler.supabase.com" not in repr(store) and "geonaf_reader" not in repr(store)
    assert "pooler.supabase.com" not in store.description and "geonaf_reader" not in store.description
