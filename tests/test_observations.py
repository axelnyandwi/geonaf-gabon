"""Acquisitions and indicator results (D3): files, fingerprints, PostGIS readers emulated, migration 0002; offline.

The real database is exercised by tests/test_postgis_storage_integration.py (marker ``postgis``).
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

import geopandas as gpd
import pytest
from shapely.geometry import box

from src.data import observations as obs
from src.data import postgis_observations as pgo
from src.data.acquisition_manifest import build_acquisition_manifest
from src.indicators.model import (
    AVAILABLE, ESTIMATION, MEDIUM, RESULT_KINDS, UNAVAILABLE, BreakdownItem, IndicatorDates, IndicatorSource,
    TerritorialIndicator,
)
from tests.test_acquisition_manifest import ASSET_IDS, _stac_item, _write_inputs

ROOT = Path(__file__).resolve().parents[1]
UP_SQL = ROOT / "db" / "sql" / "0002_observations.up.sql"
DOWN_SQL = ROOT / "db" / "sql" / "0002_observations.down.sql"
UTC = timezone.utc


# --- Acquisitions -----------------------------------------------------------------------------------


def write_product(data_dir: Path, product_id: str, sensed: str, ingested: datetime) -> Path:
    folder = data_dir / "raw" / "sentinel2" / product_id
    folder.mkdir(parents=True)
    item = _stac_item()
    item["id"], item["properties"]["datetime"] = product_id, sensed
    paths = _write_inputs(folder)
    xml = paths["calibration_xml"]
    xml.write_text(xml.read_text(encoding="utf-8").replace("S2B_SYNTHETIC_PRODUCT", product_id), encoding="utf-8")
    manifest = build_acquisition_manifest(product=item, asset_paths=paths, asset_ids=ASSET_IDS,
                                          stac_source="https://stac.example/v1", ingested_at=ingested,
                                          product_directory=folder)
    (folder / "acquisition_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return folder


@pytest.fixture
def acquisitions(tmp_path):
    write_product(tmp_path, "S2B_LATER", "2025-01-25T09:31:59.024Z", datetime(2025, 1, 26, 12, tzinfo=UTC))
    write_product(tmp_path, "S2A_EARLIER", "2025-01-20T09:33:21Z", datetime(2025, 1, 21, 8, tzinfo=UTC))
    return tmp_path


def test_manifests_are_read_with_both_dates_and_a_grid_footprint(acquisitions):
    scan = obs.FileAcquisitionCatalog(acquisitions).scan()
    assert scan.rejected == () and [r.product_id for r in scan.records] == ["S2A_EARLIER", "S2B_LATER"]
    later = scan.records[1]
    assert later.acquired_at_utc == datetime(2025, 1, 25, 9, 31, 59, 24000, tzinfo=UTC)
    assert later.ingested_at_utc == datetime(2025, 1, 26, 12, tzinfo=UTC)         # Never merged.
    assert (later.grid_crs, later.grid_resolution_m) == ("EPSG:32632", 10.0)
    footprint = later.footprint()                                                  # UTM 32N, x=500 km: lon 9°.
    assert footprint.geom_type == "Polygon" and footprint.is_valid
    left, bottom, right, top = footprint.bounds
    assert 8.999 < left < right < 9.001 and 0.90 < bottom < top < 0.91


def test_manifest_fingerprint_is_canonical_and_rejected_manifests_are_listed_not_repaired(acquisitions):
    first = obs.FileAcquisitionCatalog(acquisitions).scan()
    path = acquisitions / "raw" / "sentinel2" / "S2B_LATER" / "acquisition_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(dict(reversed(list(manifest.items())))), encoding="utf-8")    # Key order only.
    assert obs.FileAcquisitionCatalog(acquisitions).scan().records == first.records
    manifest["assets"]["b04"]["sha256"] = "not-a-digest"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    scan = obs.FileAcquisitionCatalog(acquisitions).scan()
    assert scan.rejected == ("S2B_LATER",) and [r.product_id for r in scan.records] == ["S2A_EARLIER"]


def test_ingestion_before_acquisition_is_refused(acquisitions):
    path = acquisitions / "raw" / "sentinel2" / "S2A_EARLIER" / "acquisition_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["ingested_at_utc"] = "2025-01-01T00:00:00.000000Z"
    with pytest.raises(ValueError, match="antérieure"):
        obs.acquisition_record(manifest)


def test_spatial_selection_of_acquisitions(acquisitions):
    catalog = obs.FileAcquisitionCatalog(acquisitions)
    assert len(catalog.intersecting((8.9, 0.8, 9.1, 1.0))) == 2
    assert catalog.intersecting((10, 0, 11, 1)) == ()
    assert obs.FileAcquisitionCatalog(acquisitions / "absent").scan() == obs.AcquisitionScan((), ())


def test_latest_keeps_the_most_recent_ingestion_of_each_product(acquisitions):
    scan = obs.FileAcquisitionCatalog(acquisitions).scan()
    again = obs.acquisition_record({**scan.records[0].document, "ingested_at_utc": "2025-02-01T00:00:00.000000Z"})
    combined = obs.AcquisitionScan(scan.records + (again,), ())
    latest = combined.latest()
    assert [r.product_id for r in latest] == ["S2A_EARLIER", "S2B_LATER"]
    assert latest[0].ingested_at_utc == datetime(2025, 2, 1, tzinfo=UTC)


class OfflineAcquisitions(pgo.PostgisAcquisitionCatalog):
    def __init__(self, rows):
        super().__init__(connect=None)
        self.rows = rows

    def _rows(self, query, params):
        assert query == "manifests"
        return self.rows


def test_postgis_acquisitions_are_rebuilt_from_documents_and_checked(acquisitions):
    files = obs.FileAcquisitionCatalog(acquisitions).scan()
    rows = [{"manifest_sha256": r.manifest_sha256, "manifest_document": json.loads(json.dumps(r.document))}
            for r in files.records]
    assert OfflineAcquisitions(rows).scan().records == files.records
    rows[0]["manifest_document"]["stac"]["source"] = "https://other.example"
    with pytest.raises(ValueError, match="modifié"):
        OfflineAcquisitions(rows).scan()


# --- Indicators ----------------------------------------------------------------------------------


def zone_file(folder: Path, bounds=(9.40, 0.40, 9.45, 0.45)) -> Path:
    path = folder / "zone.geojson"
    gpd.GeoDataFrame(geometry=[box(*bounds)], crs=4326).to_file(path)
    return path


def make_indicator(aoi_sha256: str, value=12.5, profile="a" * 64, status=AVAILABLE) -> TerritorialIndicator:
    dates = IndicatorDates(datetime(2025, 1, 25, 9, 31, tzinfo=UTC), datetime(2025, 1, 25, 9, 31, tzinfo=UTC),
                           "Date d'acquisition Sentinel-2", "L2A N0511", None, datetime(2025, 1, 26, 12, tzinfo=UTC))
    return TerritorialIndicator(
        indicator_id="s2_vegetation_area", label="Surface végétalisée", value=value if status == AVAILABLE else None,
        unit="km²", status=status, aoi_id="akanda", aoi_sha256=aoi_sha256, dates=dates,
        source=IndicatorSource("copernicus-sentinel-2", "Sentinel-2 L2A", "N0511", "Observation", "Copernicus", None),
        coverage_percent=97.5, method="NDVI seuillé", reliability=MEDIUM, reliability_reasons=("Couverture 97,5 %",),
        limits=("Méthode expérimentale.",), profile_fingerprint=profile, shares=(("zone", 41.2),),
        breakdown=(BreakdownItem("dense", "Dense", 3.25, "km²", 26.0, (("classe", 3.25),)),),
        result_kind=ESTIMATION, scientific_status="experimental")


def test_indicator_document_round_trip_is_exact(tmp_path):
    indicator = make_indicator("b" * 64)
    document = obs.indicator_document(indicator)
    assert json.loads(obs.canonical_json(document)) == document
    assert obs.indicator_from_document(document) == indicator
    assert obs.indicator_document(obs.indicator_from_document(document)) == document
    with pytest.raises(ValueError, match="non finie"):
        obs.indicator_document(make_indicator("b" * 64, value=float("nan")))


def test_definition_changes_with_the_profile_but_not_with_the_value():
    base = make_indicator("b" * 64)
    assert obs.definition_sha256(base) == obs.definition_sha256(make_indicator("b" * 64, value=1.0))
    assert obs.definition_sha256(base) != obs.definition_sha256(make_indicator("b" * 64, profile="c" * 64))


def test_results_are_attached_to_the_zone_geometry_without_duplicates(tmp_path):
    from src.geo.aoi_identity import load_aoi_identity

    zone = zone_file(tmp_path)
    identity = load_aoi_identity(zone)
    indicator = make_indicator(identity.file_sha256)
    store = obs.IndicatorResultStore(tmp_path / "data")
    first = store.save(indicator, zone, code_version="abc1234", computed_at=datetime(2026, 11, 5, tzinfo=UTC))
    again = store.save(indicator, zone, code_version="def5678", computed_at=datetime(2026, 11, 6, tzinfo=UTC))
    assert first == again and first.computed_at_utc.startswith("2026-11-05")      # First record kept.
    assert first.geometry_sha256 == identity.geometry_sha256
    assert len(store.store.keys("results")) == 1
    assert store.results(identity.geometry_sha256) == (first,) and store.results("0" * 64) == ()
    assert first.indicator == indicator


def test_a_result_is_never_attached_to_another_zone_file(tmp_path):
    indicator = make_indicator(obs.sha256_text("autre fichier"))
    with pytest.raises(ValueError, match="pas celui"):
        obs.IndicatorResultStore(tmp_path / "data").save(indicator, zone_file(tmp_path), code_version="x")


def test_an_edited_result_file_is_refused(tmp_path):
    from src.geo.aoi_identity import load_aoi_identity

    zone = zone_file(tmp_path)
    store = obs.IndicatorResultStore(tmp_path / "data")
    result = store.save(make_indicator(load_aoi_identity(zone).file_sha256), zone, code_version="x")
    path = store.store.path(f"results/{result.result_sha256}.json")
    path.chmod(0o644)
    path.write_text(path.read_text(encoding="utf-8").replace("12.5", "13.5"), encoding="utf-8")
    with pytest.raises(ValueError, match="modifié"):
        store.results()


def test_demo_read_only_mode_refuses_to_record_results(tmp_path, monkeypatch):
    from src.demo_mode import DemoReadOnlyError

    monkeypatch.setenv("GEONAF_DEMO_READ_ONLY", "1")
    zone = zone_file(tmp_path)
    with pytest.raises(DemoReadOnlyError):
        obs.IndicatorResultStore(tmp_path / "data").save(make_indicator("b" * 64), zone, code_version="x")


class OfflineIndicators(pgo.PostgisIndicatorStore):
    def __init__(self, rows):
        super().__init__(connect=None)
        self.rows = rows

    def _rows(self, query, params):
        return [r for r in self.rows if params["geometry_sha256"] in (None, r["result_document"]["geometry_sha256"])]


def test_postgis_indicator_results_equal_the_file_results_and_edits_are_refused(tmp_path):
    from src.geo.aoi_identity import load_aoi_identity

    zone = zone_file(tmp_path)
    store = obs.IndicatorResultStore(tmp_path / "data")
    sha = load_aoi_identity(zone).file_sha256
    for status in (AVAILABLE, UNAVAILABLE):
        store.save(make_indicator(sha, status=status), zone, code_version="x")
    rows = [{"result_sha256": r.result_sha256, "result_document": json.loads(json.dumps(r.document))}
            for r in store.results()]
    assert OfflineIndicators(rows).results() == store.results()
    rows[0]["result_document"]["indicator"]["value"] = 99.0
    with pytest.raises(ValueError):
        OfflineIndicators(rows).results()
    with pytest.raises(PermissionError):
        OfflineIndicators(rows).save()


# --- Migration 0002 (static) ---------------------------------------------------------------------------


def _columns(sql: str, table: str) -> list[str]:
    body = sql.split(f"CREATE TABLE geonaf.{table} (")[1].split("\n);")[0]
    return [m.group(1) for m in re.finditer(r"^  ([a-z_0-9]+)\s+(?!KEY)", body, re.M)
            if m.group(1) not in ("primary", "foreign", "check", "unique")]


def test_rows_built_in_python_have_the_columns_of_the_tables(tmp_path):
    sql = UP_SQL.read_text(encoding="utf-8")
    from src.geo.aoi_identity import load_aoi_identity

    zone = zone_file(tmp_path)
    result = obs.IndicatorResultStore(tmp_path / "data").save(make_indicator(load_aoi_identity(zone).file_sha256),
                                                              zone, code_version="x")
    assert sorted([*pgo.result_row(result), "import_id"]) == sorted(_columns(sql, "indicator_results"))
    insert = re.search(r"INSERT INTO geonaf\.acquisition_manifests \(([^)]*)\)",
                       (ROOT / "src" / "data" / "postgis_observations.py").read_text(encoding="utf-8")).group(1)
    assert sorted(c.strip() for c in insert.replace('"', "").split(",")) == \
        sorted(_columns(sql, "acquisition_manifests"))


def test_migration_0002_follows_the_conventions_of_0001():
    for path in (UP_SQL, DOWN_SQL):
        text = path.read_text(encoding="utf-8")
        assert "%" not in text and "—" not in text and "CREATE EXTENSION" not in text
    sql = UP_SQL.read_text(encoding="utf-8")
    tables = re.findall(r"CREATE TABLE geonaf\.(\w+)", sql)
    assert set(tables) == {"data_imports", "acquisitions", "acquisition_manifests", "indicator_result_kinds",
                           "indicator_definitions", "indicator_results"}
    for table in tables:
        assert f"BEFORE TRUNCATE ON geonaf.{table}" in sql
        assert re.search(rf"BEFORE UPDATE OR DELETE ON geonaf\.{table}\s", sql)
        assert f"ALTER TABLE geonaf.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"geonaf.{table}" in sql.split("FOREACH api_role")[1].split("END LOOP")[0] or \
            table in sql.split("FOREACH geonaf_table")[1]
    assert not re.search(r"^\s*SET\s", sql, re.M | re.I)
    assert sql.count("CREATE FUNCTION") == sql.count("LANGUAGE plpgsql SET search_path = '' AS $$") == 2
    assert sql.count("WITH (security_invoker = true)") == sql.count("CREATE VIEW") == 1
    kinds = re.findall(r"\('((?:[^']|'')+)'\)", sql.split("INSERT INTO geonaf.indicator_result_kinds")[1]
                       .split(";")[0])
    assert tuple(k.replace("''", "'") for k in kinds) == RESULT_KINDS
    assert "'NaN', 'Infinity', '-Infinity'" in sql                       # NaN = NaN is true in PostgreSQL.
    down = DOWN_SQL.read_text(encoding="utf-8")
    assert "DROP SCHEMA" not in down and all(f"geonaf.{t}" in down for t in tables)


def test_reliability_and_status_copies_match_python():
    from src.indicators import model

    sql = UP_SQL.read_text(encoding="utf-8")
    assert f"status IN ('{model.AVAILABLE}', '{model.UNAVAILABLE}')" in sql
    assert ("reliability IN ('" + "', '".join((model.HIGH, model.MEDIUM, model.LOW, model.NOT_ASSESSED)) + "')") \
        in sql
    assert "scientific_status IN ('experimental', 'validated')" in sql
    from src.indicators.profiles import EXPERIMENTAL, VALIDATED

    assert (EXPERIMENTAL, VALIDATED) == ("experimental", "validated")
    assert "'geonaf.sentinel2-acquisition/v1'" in sql


# --- Import tool, offline ----------------------------------------------------------------------------


def run_tool(argv, monkeypatch, capsys):
    import sys

    sys.path.insert(0, str(ROOT))
    from scripts import import_observations_postgis as tool

    monkeypatch.delenv("GEONAF_DATABASE_URL", raising=False)
    code = tool.main(argv)
    output = capsys.readouterr()
    return code, output.out, output.err


def test_import_tool_verifies_offline_and_never_prints_paths(acquisitions, monkeypatch, capsys):
    from src.geo.aoi_identity import load_aoi_identity

    zone = zone_file(acquisitions)
    obs.IndicatorResultStore(acquisitions).save(make_indicator(load_aoi_identity(zone).file_sha256), zone,
                                                code_version="t")
    code, out, err = run_tool(["--data-dir", str(acquisitions), "--source-label", "poste local"], monkeypatch, capsys)
    assert code == 0 and err == ""
    assert "2 manifestes, 2 produits" in out and "1 résultats, 1 définitions, 1 géométries" in out
    assert "aucune connexion" in out and str(acquisitions) not in out and "pytest" not in out
    code, _, err = run_tool(["--data-dir", str(acquisitions), "--source-label", "poste local", "--apply"],
                            monkeypatch, capsys)
    assert code == 2 and "--apply exige" in err
    code, _, err = run_tool(["--data-dir", str(acquisitions), "--source-label", "C:\donnees"], monkeypatch, capsys)
    assert code == 2


def test_import_tool_refuses_a_partial_batch(acquisitions, monkeypatch, capsys):
    (acquisitions / "raw" / "sentinel2" / "S2A_EARLIER" / "acquisition_manifest.json").write_text("{", encoding="utf-8")
    code, out, err = run_tool(["--data-dir", str(acquisitions), "--source-label", "poste", "--scope", "acquisitions"],
                              monkeypatch, capsys)
    assert code == 1 and "S2A_EARLIER" in err and "aucun import partiel" in err


def test_latest_breaks_equal_ingestion_dates_by_the_greatest_digest_like_the_view(acquisitions):
    """Parity with geonaf.acquisitions_latest (ORDER BY ingested_at DESC, manifest_sha256 DESC)."""
    record = obs.FileAcquisitionCatalog(acquisitions).scan().records[0]
    twin = obs.acquisition_record({**record.document, "stac": {**record.document["stac"],
                                                                "source": "https://stac-bis.example/v1"}})
    assert twin.ingested_at_utc == record.ingested_at_utc and twin.manifest_sha256 != record.manifest_sha256
    for order in ((record, twin), (twin, record)):
        latest = obs.AcquisitionScan(order, ()).latest()
        assert [r.manifest_sha256 for r in latest] == [max(record.manifest_sha256, twin.manifest_sha256)]
