"""PostGIS territory registry without a database: geometry conversion, rows, import plan, SQL files; offline.

The database itself is exercised by tests/test_postgis_territory_integration.py (marker ``postgis``).
Here the repository's SQL is replaced by an in-memory emulation of the same queries, so that the
row mapping and the record reconstruction are compared with the file registry.
"""

from datetime import datetime, timezone
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon, box

from src.data import postgis_territory_registry as pg
from src.data.territory_registry import (
    ACQUISITION_ZONE, ADMIN_UNIT, ADMINISTRATIVE_WORDS, COMMUNITY, GEONAF_GRID, GEONAF_ZONE, GRID_CELL, HUMANITARIAN,
    KINDS, LEGAL_STATUSES, AdminInfo, GridInfo, TerritoryRegistry, TerritorySource,
)
from src.geo.aoi_identity import geometry_fingerprint, load_aoi_identity
from tests.test_aoi_identity import L_SHAPE, utm, write_geojson

ROOT = Path(__file__).resolve().parents[1]
UP_SQL = ROOT / "db" / "sql" / "0001_territorial_registry.up.sql"
DOWN_SQL = ROOT / "db" / "sql" / "0001_territorial_registry.down.sql"
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
GBOPEN = TerritorySource("geoboundaries-gbopen", "geoBoundaries gbOpen GAB ADM2", "2023-12-12", "CC-BY-3.0",
                         "geoBoundaries, Wikimedia", "https://www.geoboundaries.org", reference_year=2018,
                         retrieved_at_utc="2026-10-23T17:02:36.526974+00:00")
GBHUMANITARIAN = TerritorySource("geoboundaries-gbhumanitarian", "geoBoundaries gbHumanitarian GAB ADM2",
                                 "2023-12-12", "CC-BY-3.0-IGO", "OCHA COD-AB / SALB", reference_year=2015)
GEONAF = TerritorySource("geonaf", "Zones d'acquisition GEONAF", "2026.1", "LicenseRef-GEONAF", "GEONAF")
DEPARTMENT = AdminInfo("GAB", "ADM2", "Department", "GAB-ADM2-x")


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def registry(tmp_path):
    """Every kind, one geometry shared by two referentials, a MultiPolygon, a polygon with a hole, two versions."""
    files = tmp_path / "files"
    files.mkdir()
    reg = TerritoryRegistry(tmp_path / "data")
    square = write_geojson(files / "square.geojson", [box(9.40, 0.40, 9.45, 0.45)])
    islands = write_geojson(files / "islands.geojson", [box(9.30, 0.30, 9.32, 0.32), box(9.34, 0.30, 9.36, 0.32)])
    holed = write_geojson(files / "holed.geojson", [Polygon(box(9.5, 0.5, 9.6, 0.6).exterior.coords,
                                                            [box(9.52, 0.52, 9.54, 0.54).exterior.coords])])
    for territory_id, source, status in (("gab-gbopen-adm2-a", GBOPEN, COMMUNITY),
                                         ("gab-gbhumanitarian-adm2-a", GBHUMANITARIAN, HUMANITARIAN)):
        reg.register(territory_id=territory_id, label="Unité A", kind=ADMIN_UNIT, legal_status=status, source=source,
                     geometry_path=square, admin=DEPARTMENT, limits=("Référentiel non officiel.",), now=NOW)
    reg.register(territory_id="gab-gbopen-adm2-iles", label="Îles", kind=ADMIN_UNIT, legal_status=COMMUNITY,
                 source=GBOPEN, geometry_path=islands, admin=DEPARTMENT, now=NOW)
    reg.register(territory_id="akanda", label="Zone d'acquisition Akanda (12 × 12 km)", kind=ACQUISITION_ZONE,
                 legal_status=GEONAF_ZONE, source=GEONAF, geometry_path=square, now=NOW)
    reg.register(territory_id="akanda", label="Zone d'acquisition Akanda (12 × 12 km)", kind=ACQUISITION_ZONE,
                 legal_status=GEONAF_ZONE, source=GEONAF, geometry_path=holed, now=NOW)       # Version 2.
    reg.register(territory_id="grid-0-0", label="Maille 0-0", kind=GRID_CELL, legal_status=GEONAF_GRID,
                 source=GEONAF, geometry_path=holed, grid=GridInfo("geonaf-wgs84-0.05deg", "0-0", 0.05), now=NOW)
    return reg


def database_rows(plan: pg.ImportPlan) -> list[dict]:
    """Rows as SELECTed by the repository: version columns, release columns, document through JSON (jsonb)."""
    rows = []
    for record in plan.records:
        row = pg.version_row(record)
        row["record_document"] = json.loads(json.dumps(row["record_document"]))
        source = record.source
        row.update(source_id=source.source_id, product=source.product, source_version=source.version,
                   licence_id=source.license_spdx, attribution=source.attribution, url=source.url,
                   share_alike=source.share_alike, reference_year=source.reference_year)
        rows.append(row)
    return rows


class OfflineRegistry(pg.PostgisTerritoryRegistry):
    """Emulates the SQL of pg.QUERIES on rows built from an import plan."""

    def __init__(self, plan, data_dir, rows=None):
        super().__init__(connect=None, data_dir=data_dir)
        self.plan, self.table = plan, rows if rows is not None else database_rows(plan)

    def _rows(self, query, params):
        latest = {}
        for row in self.table:
            if row["version"] > latest.get(row["territory_id"], {"version": 0})["version"]:
                latest[row["territory_id"]] = row
        ordered = [latest[key] for key in sorted(latest)]
        if query == "versions":
            return sorted((r for r in self.table if r["territory_id"] == params["territory_id"]),
                          key=lambda r: r["version"])
        if query == "latest":
            return [r for r in ordered if params["kind"] is None or r["kind"] == params["kind"]]
        if query == "latest_by_geometry":
            return [r for r in ordered if r["geometry_sha256"] == params["geometry_sha256"]]
        if query == "latest_intersecting":
            rectangle = box(params["west"], params["south"], params["east"], params["north"])
            return [r for r in ordered if (params["kind"] is None or r["kind"] == params["kind"])
                    and shapely.from_wkb(self.plan.geometries[r["geometry_sha256"]].wkb).intersects(rectangle)]
        if query == "geometry":
            stored = self.plan.geometries.get(params["geometry_sha256"])
            return [] if stored is None else [{"source_geometry_type": stored.source_geometry_type, "wkb": stored.wkb}]
        raise AssertionError(query)


# --- Geometry --------------------------------------------------------------------------------------


@pytest.mark.parametrize("geometry", [
    box(9.40, 0.40, 9.45, 0.45),
    Polygon(box(0, 0, 1, 1).exterior.coords, [box(0.2, 0.2, 0.4, 0.4).exterior.coords]),
    MultiPolygon([box(0, 0, 1, 1), box(2, 0, 3, 1), box(4, 0, 5, 1)]),
    utm(L_SHAPE),
])
def test_polygon_to_multipolygon_conversion_keeps_the_fingerprint(geometry):
    stored, source_type = pg.to_stored_geometry(geometry)
    assert stored.geom_type == "MultiPolygon" and source_type == geometry.geom_type
    back = pg.from_stored_geometry(shapely.from_wkb(shapely.to_wkb(stored, byte_order=1)), source_type)
    assert back.geom_type == geometry.geom_type and back.equals_exact(geometry, 0)
    assert geometry_fingerprint(back) == geometry_fingerprint(geometry)
    if geometry.geom_type == "Polygon":   # Without the original type the fingerprint would change.
        assert geometry_fingerprint(stored) != geometry_fingerprint(geometry)


def test_unsupported_geometries_are_refused():
    with pytest.raises(ValueError, match="3D"):
        pg.to_stored_geometry(Polygon([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1)]))
    with pytest.raises(ValueError, match="surfacique"):
        pg.to_stored_geometry(Point(0, 0))
    with pytest.raises(ValueError, match="plusieurs parties"):
        pg.from_stored_geometry(MultiPolygon([box(0, 0, 1, 1), box(2, 0, 3, 1)]), "Polygon")
    with pytest.raises(ValueError, match="inconnu"):
        pg.from_stored_geometry(MultiPolygon([box(0, 0, 1, 1)]), "GeometryCollection")


def test_union_is_the_one_fingerprinted_by_aoi_identity(tmp_path):
    files = [write_geojson(tmp_path / "a.geojson", [box(9.4, 0.4, 9.45, 0.45)]),
             write_geojson(tmp_path / "b.geojson", [box(9.4, 0.4, 9.45, 0.45), box(9.45, 0.4, 9.5, 0.45)]),
             write_geojson(tmp_path / "c.geojson", [L_SHAPE], crs="EPSG:32632")]
    for path in files:
        identity = load_aoi_identity(path)
        union = pg.wgs84_union(path)
        assert geometry_fingerprint(union) == identity.geometry_sha256
        assert int(shapely.get_num_coordinates(union)) == identity.vertices


# --- Rows and import plan ------------------------------------------------------------------------


def test_plan_verifies_the_registry_without_writing(registry):
    before = snapshot(registry.data_dir)
    plan = pg.plan_import(registry)
    assert snapshot(registry.data_dir) == before
    assert [(r.territory_id, r.version) for r in plan.records] == [
        ("akanda", 1), ("akanda", 2), ("gab-gbhumanitarian-adm2-a", 1), ("gab-gbopen-adm2-a", 1),
        ("gab-gbopen-adm2-iles", 1), ("grid-0-0", 1)]
    assert len(plan.geometries) == 3                                       # Square shared by three records.
    assert sorted(g.source_geometry_type for g in plan.geometries.values()) == ["MultiPolygon", "Polygon", "Polygon"]
    assert plan.licence_ids == ("CC-BY-3.0", "CC-BY-3.0-IGO", "LicenseRef-GEONAF")
    assert len(plan.releases) == 3
    assert plan.fingerprints_sha256 == pg.plan_import(registry).fingerprints_sha256   # Deterministic.


def test_plan_refuses_a_modified_geometry_file_or_record(registry):
    record = registry.get("gab-gbopen-adm2-iles")
    path = registry.geometry_path(record)
    path.write_bytes(path.read_bytes().replace(b"9.3", b"9.2", 1))
    with pytest.raises(ValueError, match="modifiée"):
        pg.plan_import(registry)


def test_plan_refuses_a_modified_record(registry):
    path = registry.store.path("records/grid-0-0/v0001.json")
    path.write_text(path.read_text(encoding="utf-8").replace("Maille 0-0", "Maille 0-1"), encoding="utf-8")
    with pytest.raises(ValueError, match="modifié"):
        pg.plan_import(registry)


def test_version_row_keeps_every_field_and_the_fingerprint_survives_jsonb(registry):
    plan = pg.plan_import(registry)
    for record in plan.records:
        row = pg.version_row(record)
        assert row["record_fingerprint"] == record.fingerprint
        assert set(row["record_document"]) == set(record.__dataclass_fields__)
        rebuilt = pg.record_from_row(database_rows(pg.ImportPlan((record,), plan.geometries))[0])
        assert rebuilt == record and rebuilt.fingerprint == record.fingerprint
    zone = registry.get("akanda", 2)
    assert pg.version_row(zone)["country_iso3"] is None and pg.version_row(zone)["supersedes"] == \
        registry.get("akanda", 1).fingerprint
    cell = pg.version_row(registry.get("grid-0-0"))
    assert (cell["grid_system"], cell["grid_cell_id"], cell["grid_cell_size_deg"]) == ("geonaf-wgs84-0.05deg", "0-0",
                                                                                     0.05)
    unit = pg.version_row(registry.get("gab-gbopen-adm2-a"))
    assert (unit["country_iso3"], unit["admin_level_code"], unit["local_type"]) == ("GAB", "ADM2", "Department")
    assert unit["retrieved_at_utc"] == GBOPEN.retrieved_at_utc and unit["limits"] == ["Référentiel non officiel."]


# --- Read-only repository ------------------------------------------------------------------------


def assert_parity(files: TerritoryRegistry, database: pg.PostgisTerritoryRegistry):
    for kind in (None, *KINDS):
        assert database.territories(kind) == files.territories(kind)
    for record in files.territories():
        assert database.versions(record.territory_id) == files.versions(record.territory_id)
        assert database.latest(record.territory_id) == files.latest(record.territory_id)
        assert database.get(record.territory_id, 1) == files.get(record.territory_id, 1)
        assert database.find_geometry(record.geometry_sha256) == files.find_geometry(record.geometry_sha256)
        assert geometry_fingerprint(database.geometry(record)) == record.geometry_sha256
        assert database.geometry_path(record) == files.geometry_path(record)


def test_offline_repository_reads_exactly_what_the_file_registry_reads(registry):
    database = OfflineRegistry(pg.plan_import(registry), registry.data_dir)
    assert_parity(registry, database)
    assert database.latest("inconnu") is None
    with pytest.raises(KeyError, match="v9"):
        database.get("akanda", 9)
    with pytest.raises(ValueError, match="invalide"):
        database.versions("Akanda!")
    assert [r.territory_id for r in database.intersecting((9.39, 0.39, 9.41, 0.41))] == [
        "gab-gbhumanitarian-adm2-a", "gab-gbopen-adm2-a"]                 # akanda v2 has moved (holed square).
    assert [r.territory_id for r in database.intersecting((9.39, 0.39, 9.41, 0.41), kind=ACQUISITION_ZONE)] == []


def test_rows_edited_in_the_database_are_refused(registry):
    plan = pg.plan_import(registry)
    edits = [lambda row: row["record_document"].update(label="Commune A"),       # Document: fingerprint differs.
             lambda row: row.update(label="Autre libellé"),                      # Column differs from the document.
             lambda row: row.update(attribution="Autre attribution"),            # Release differs.
             lambda row: row.update(retrieved_at_utc=None)]
    for edit in edits:
        rows = database_rows(plan)
        edit(next(r for r in rows if r["territory_id"] == "gab-gbopen-adm2-a"))
        with pytest.raises(ValueError):
            OfflineRegistry(plan, registry.data_dir, rows).territories()


def test_postgis_registry_is_read_only(registry):
    with pytest.raises(pg.ReadOnlyRegistryError):
        OfflineRegistry(pg.plan_import(registry), registry.data_dir).register(territory_id="x")


def test_conninfo_drops_the_sqlalchemy_driver_prefix():
    assert pg.psycopg_conninfo("postgresql+psycopg://u@127.0.0.1:5433/geonaf") == "postgresql://u@127.0.0.1:5433/geonaf"
    assert pg.psycopg_conninfo("postgresql://h/db") == "postgresql://h/db"
    with pytest.raises(ValueError):
        pg.psycopg_conninfo("sqlite:///x.db")


# --- SQL files and migrations --------------------------------------------------------------------


def test_sql_copies_of_python_rules_are_identical():
    sql = UP_SQL.read_text(encoding="utf-8")
    statuses = set(re.findall(r"\('(\w+)', '((?:[^']|'')+)'\)", sql.split("INSERT INTO geonaf.legal_statuses")[1]
                              .split(";")[0]))
    assert {(kind, status.replace("''", "'")) for kind, status in statuses} == {
        (kind, status) for kind, allowed in LEGAL_STATUSES.items() for status in allowed}
    kinds = re.findall(r"\('(\w+)'\)", sql.split("INSERT INTO geonaf.territory_kinds")[1].split(";")[0])
    assert tuple(sorted(kinds)) == tuple(sorted(KINDS))
    words = re.search(r"lower\(label\) !~\s*'\(([^)]*)\)'", sql).group(1).split("|")
    assert tuple(words) == ADMINISTRATIVE_WORDS
    assert "'^[a-z0-9][a-z0-9_-]{1,63}$'" in sql                         # Same identifier rule as _ID.
    assert "schema = 'geonaf.territory/v1'" in sql and "'geonaf-aoi-geometry-v1'" in sql


def test_sql_files_are_safe_to_send_to_the_driver_and_every_table_is_immutable():
    for path in (UP_SQL, DOWN_SQL, ROOT / "db" / "sql" / "roles.sql"):
        text = path.read_text(encoding="utf-8")
        assert "%" not in text and "\u2014" not in text and not re.search(r"PASSWORD\s+'", text, re.I)
    sql = UP_SQL.read_text(encoding="utf-8")
    tables = re.findall(r"CREATE TABLE geonaf\.(\w+)", sql)
    assert set(tables) == {"licences", "data_sources", "source_releases", "geometries", "territory_kinds",
                           "legal_statuses", "territories", "registry_imports", "territory_versions"}
    for table in tables:
        assert f"BEFORE TRUNCATE ON geonaf.{table}" in sql
        assert re.search(rf"BEFORE UPDATE OR DELETE ON geonaf\.{table}\s", sql)
    assert "ST_MakeValid" not in sql and "ST_Area" not in sql and "CREATE EXTENSION" not in sql
    # Session settings are never changed by the body; every function runs with an empty search_path.
    assert not re.search(r"^\s*SET\s", sql, re.M | re.I)
    assert sql.count("CREATE FUNCTION") == sql.count("LANGUAGE plpgsql SET search_path = '' AS $$") == 3
    assert sql.count("WITH (security_invoker = true)") == sql.count("CREATE VIEW") == 2
    assert len(re.findall(r"ALTER TABLE geonaf\.\w+ ENABLE ROW LEVEL SECURITY", sql)) == len(tables)
    assert "geometry(MultiPolygon, 4326)" in sql and "USING gist (geom)" in sql
    assert DOWN_SQL.read_text(encoding="utf-8").strip().endswith("DROP SCHEMA geonaf CASCADE;")


def test_licences_used_by_the_registry_are_seeded_by_the_migration():
    sql = UP_SQL.read_text(encoding="utf-8").split("INSERT INTO geonaf.licences")[1].split(";")[0]
    assert set(re.findall(r"\('([A-Za-z0-9.+-]+)',", sql)) == {"CC-BY-3.0", "CC-BY-3.0-IGO", "LicenseRef-GEONAF"}


def test_migration_chain_points_to_existing_sql_files():
    versions = sorted((ROOT / "db" / "migrations" / "versions").glob("*.py"))
    assert [p.name for p in versions] == ["0001_territorial_registry.py", "0002_observations.py"]
    expected = {"0001_territorial_registry": None, "0002_observations": "0001_territorial_registry"}
    for path in versions:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = {node.targets[0].id: node.value.value for node in tree.body
                  if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)}
        assert values["revision"] == path.stem and values["down_revision"] == expected[path.stem]
        names = re.findall(r'"(\d{4}_[a-z_]+\.(?:up|down)\.sql)"', path.read_text(encoding="utf-8"))
        assert sorted(names) == [f"{path.stem}.down.sql", f"{path.stem}.up.sql"]
        assert all((ROOT / "db" / "sql" / name).is_file() for name in names)
    ini = (ROOT / "db" / "alembic.ini").read_text(encoding="utf-8")
    assert "sqlalchemy.url" not in ini and "postgresql" not in ini      # No URL, hence no secret, in the file.


# --- File backend and dependencies ---------------------------------------------------------------


def test_file_backend_never_imports_the_database_driver():
    code = ("import sys, src.data.postgis_territory_registry, src.services.territory_catalog, "
            "src.components.territory_explorer; print(any(m.split('.')[0] in ('psycopg', 'sqlalchemy', 'alembic') "
            "for m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "False"
    users = [p.relative_to(ROOT).as_posix() for p in (ROOT / "src").rglob("*.py")
             if re.search(r"^(import|from)\s+(psycopg|sqlalchemy|alembic)", p.read_text(encoding="utf-8"), re.M)]
    assert users == []                       # Imported lazily, inside functions of the PostGIS module only.
    for name in ("postgis_territory_registry.py", "postgis_session.py", "postgis_observations.py"):
        lazy = (ROOT / "src" / "data" / name).read_text(encoding="utf-8")
        assert re.search(r"^ {4,}import psycopg$", lazy, re.M), name        # Inside a function only.


def test_database_dependencies_stay_out_of_requirements_txt():
    base = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert not any(name in base for name in ("psycopg", "alembic", "sqlalchemy"))
    db = (ROOT / "requirements-db.txt").read_text(encoding="utf-8").splitlines()
    assert "-r requirements.txt" in db
    pinned = [line for line in db if line and not line.startswith(("#", "-r"))]
    assert pinned and all("==" in line for line in pinned)
    assert {line.split("==")[0].split("[")[0].lower() for line in pinned} == {"psycopg", "psycopg-pool", "sqlalchemy",
                                                                              "alembic"}


# --- Import script, offline ----------------------------------------------------------------------


def run_script(argv, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import import_territories_postgis as script
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    monkeypatch.delenv(pg.URL_VARIABLE, raising=False)
    code = script.main(argv)
    return code, capsys.readouterr()


def test_import_script_verifies_offline_and_never_prints_paths(registry, monkeypatch, capsys):
    before = snapshot(registry.data_dir)
    code, out = run_script(["--data-dir", str(registry.data_dir), "--source-label", "registre de test"],
                           monkeypatch, capsys)
    assert code == 0 and snapshot(registry.data_dir) == before
    assert "6 versions, 3 géométries, 3 versions de source" in out.out
    assert "aucune connexion" in out.out
    assert str(registry.data_dir) not in out.out + out.err and "\\" not in out.out
    code, out = run_script(["--data-dir", str(registry.data_dir), "--source-label", "registre", "--apply"],
                           monkeypatch, capsys)
    assert code == 2 and pg.URL_VARIABLE in out.err                       # --apply needs a database.
    code, out = run_script(["--data-dir", str(registry.data_dir), "--source-label", "C:/data"], monkeypatch, capsys)
    assert code == 2 and "chemin" in out.err


# --- Real registry (paquet v3, read only) --------------------------------------------------------


def real_registry_dir():
    from tests.test_s2_vegetation_inventory import PACK

    return PACK if (PACK / "territories" / "records").is_dir() else None


@pytest.mark.skipif(real_registry_dir() is None, reason="Registre du paquet de démonstration absent.")
def test_real_registry_plan_and_offline_parity():
    """The 97 geoBoundaries units: every fingerprint survives the MultiPolygon form; nothing is written."""
    data_dir = real_registry_dir()
    files = TerritoryRegistry(data_dir)
    before = snapshot(data_dir / "territories")
    plan = pg.plan_import(files)
    assert len(plan.records) == len(plan.geometries) == 97
    assert sorted(g.source_geometry_type for g in plan.geometries.values()).count("MultiPolygon") == 5
    assert plan.licence_ids == ("CC-BY-3.0", "CC-BY-3.0-IGO") and len(plan.releases) == 2
    assert_parity(files, OfflineRegistry(plan, data_dir))
    assert snapshot(data_dir / "territories") == before
