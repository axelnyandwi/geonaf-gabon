"""Territory registry: immutability, versions, legal statuses, historical zones; offline."""

from datetime import datetime, timezone
import json
import shutil

import pytest
from shapely.geometry import MultiPolygon, box

from src.data.territory_registry import (
    ACQUISITION_ZONE, ADMIN_UNIT, COMMUNITY, CUSTOM_IMPORT, GEONAF_GRID, GEONAF_ZONE, GRID_CELL, HUMANITARIAN,
    OFFICIAL_VERIFIED, USER_PROVIDED, AdminInfo, GridInfo, TerritoryRegistry, TerritorySource,
    register_historical_zones,
)
from src.geo.aoi_identity import load_aoi_identity
from tests.test_aoi_identity import L_SHAPE, utm, write_geojson

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
GBOPEN = TerritorySource("geoboundaries-gbopen", "geoBoundaries gbOpen GAB ADM2", "2023-12-12", "CC-BY-3.0",
                         "geoBoundaries, Wikimedia", "https://www.geoboundaries.org", reference_year=2018)
GBHUMANITARIAN = TerritorySource("geoboundaries-gbhumanitarian", "geoBoundaries gbHumanitarian GAB ADM2",
                                 "2023-12-12", "CC-BY-3.0-IGO", "OCHA COD-AB / SALB", reference_year=2015)
DEPARTMENT = AdminInfo("GAB", "ADM2", "département", "GAB-ADM2-x")


@pytest.fixture
def registry(tmp_path):
    return TerritoryRegistry(tmp_path / "data")


@pytest.fixture
def square(tmp_path):
    return write_geojson(tmp_path / "square.geojson", [box(9.40, 0.40, 9.45, 0.45)])


def admin(registry, path, territory_id="gab-gbopen-adm2-libreville", source=GBOPEN, status=COMMUNITY, **kw):
    return registry.register(territory_id=territory_id, label="Libreville", kind=ADMIN_UNIT, legal_status=status,
                             source=source, geometry_path=path, admin=DEPARTMENT, now=NOW, **kw)


def test_register_is_idempotent_and_records_identity(registry, square):
    record = admin(registry, square)
    assert record.version == 1 and record.supersedes is None
    assert record.geometry_sha256 == load_aoi_identity(square).geometry_sha256
    assert record.area_km2 == pytest.approx(30.8, rel=0.01) and (record.vertices, record.parts) == (5, 1)
    assert admin(registry, square) == record                       # Same content: no new version.
    assert registry.get("gab-gbopen-adm2-libreville") == record
    assert registry.geometry_path(record).read_bytes() == square.read_bytes()


def test_geometry_or_metadata_change_creates_a_new_version(registry, square, tmp_path):
    first = admin(registry, square)
    moved = write_geojson(tmp_path / "moved.geojson", [box(9.40, 0.40, 9.46, 0.45)])
    second = admin(registry, moved)
    assert (second.version, second.supersedes) == (2, first.fingerprint)
    assert second.geometry_sha256 != first.geometry_sha256
    third = admin(registry, moved, limits=("Limite approximative.",))
    assert third.version == 3 and third.geometry_sha256 == second.geometry_sha256
    assert [r.version for r in registry.versions("gab-gbopen-adm2-libreville")] == [1, 2, 3]
    assert registry.get("gab-gbopen-adm2-libreville", 1) == first      # Earlier versions stay readable.
    assert registry.geometry_path(first).exists() and registry.geometry_path(second).exists()


def test_nothing_is_overwritten_and_tampering_is_detected(registry, square):
    record = admin(registry, square)
    record_file = registry.store.path("records/gab-gbopen-adm2-libreville/v0001.json")
    with pytest.raises(FileExistsError):
        registry.store.write_bytes("records/gab-gbopen-adm2-libreville/v0001.json", b"{}")
    payload = json.loads(record_file.read_text(encoding="utf-8"))
    payload["record"]["legal_status"] = HUMANITARIAN
    record_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="modifié"):
        registry.versions("gab-gbopen-adm2-libreville")
    geometry = registry.store.path(record.storage_key)
    geometry.write_bytes(geometry.read_bytes() + b" ")
    with pytest.raises(ValueError, match="modifiée"):
        registry.geometry_path(record)


def test_two_referentials_stay_two_territories(registry, square, tmp_path):
    larger = write_geojson(tmp_path / "larger.geojson", [box(9.38, 0.38, 9.47, 0.47)])
    open_ = admin(registry, square)
    humanitarian = admin(registry, larger, territory_id="gab-gbhumanitarian-adm2-libreville",
                         source=GBHUMANITARIAN, status=HUMANITARIAN)
    assert {r.territory_id for r in registry.territories(ADMIN_UNIT)} == {open_.territory_id,
                                                                         humanitarian.territory_id}
    assert not open_.is_official and not humanitarian.is_official
    assert registry.find_geometry(open_.geometry_sha256) == (open_,)


@pytest.mark.parametrize("kind, status, extra, message", [
    (ADMIN_UNIT, OFFICIAL_VERIFIED, {}, "justificatif"),                         # Official without evidence.
    (ADMIN_UNIT, OFFICIAL_VERIFIED, {"legal_evidence": "  "}, "justificatif"),
    (ADMIN_UNIT, GEONAF_ZONE, {}, "interdit"),
    (ADMIN_UNIT, COMMUNITY, {"admin": None}, "niveau"),
    (ACQUISITION_ZONE, OFFICIAL_VERIFIED, {"admin": None}, "interdit"),
    (ACQUISITION_ZONE, GEONAF_ZONE, {}, "pas une unité administrative"),         # Zone with admin info.
    (CUSTOM_IMPORT, COMMUNITY, {}, "interdit"),
    (GRID_CELL, GEONAF_GRID, {"admin": None}, "grille"),                         # Grid without grid info.
])
def test_inconsistent_statuses_are_refused(registry, square, kind, status, extra, message):
    arguments = dict(territory_id="t-1", label="Libreville", kind=kind, legal_status=status, source=GBOPEN,
                     geometry_path=square, admin=DEPARTMENT, now=NOW)
    arguments.update(extra)
    with pytest.raises(ValueError, match=message):
        registry.register(**arguments)
    assert registry.territories() == ()


@pytest.mark.parametrize("label", ["Commune d'Akanda", "Département de Libreville", "Arrondissement 1"])
def test_acquisition_zone_never_named_like_an_administrative_unit(registry, square, label):
    with pytest.raises(ValueError, match="unité administrative"):
        registry.register(territory_id="zone-x", label=label, kind=ACQUISITION_ZONE, legal_status=GEONAF_ZONE,
                          source=GBOPEN, geometry_path=square, now=NOW)


def test_official_limit_requires_and_keeps_its_evidence(registry, square):
    record = registry.register(
        territory_id="gab-import-akanda", label="Akanda", kind=CUSTOM_IMPORT, legal_status=OFFICIAL_VERIFIED,
        source=TerritorySource("user-import", "Fichier partenaire", "2026-10-23", "LicenseRef-partenaire",
                               "Partenaire"),
        geometry_path=square, admin=AdminInfo("GAB", "commune", "commune"),
        legal_evidence="Décret n° 701/PR/MISPID du 17/07/2013, annexe cartographique fournie", now=NOW)
    assert record.is_official and "701" in record.legal_evidence


def test_grid_cell_and_user_import_kinds(registry, square):
    cell = registry.register(
        territory_id="grid-005-e0940-n0040", label="Maille 0,05° E9,40 N0,40", kind=GRID_CELL,
        legal_status=GEONAF_GRID, source=TerritorySource("geonaf", "Grille GEONAF", "1", "LicenseRef-GEONAF", "GEONAF"),
        geometry_path=square, grid=GridInfo("geonaf-wgs84-0.05deg", "E0940N0040", 0.05), now=NOW)
    imported = registry.register(
        territory_id="import-zone", label="Zone importée", kind=CUSTOM_IMPORT, legal_status=USER_PROVIDED,
        source=TerritorySource("user-import", "Fichier", "1", "CC-BY-4.0", "Auteur"), geometry_path=square, now=NOW)
    assert cell.geometry_sha256 == imported.geometry_sha256 and len(registry.find_geometry(cell.geometry_sha256)) == 2
    assert registry.territories(GRID_CELL) == (cell,)


@pytest.mark.parametrize("bad_id", ["Akanda", "../x", "a", "a b", "x" * 70])
def test_identifiers_are_safe(registry, square, bad_id):
    with pytest.raises(ValueError, match="Identifiant"):
        admin(registry, square, territory_id=bad_id)


def test_invalid_geometry_is_refused_and_not_stored(registry, tmp_path):
    from shapely.geometry import Polygon

    bow_tie = write_geojson(tmp_path / "bad.geojson", [Polygon([(9.3, 0.4), (9.4, 0.5), (9.4, 0.4), (9.3, 0.5)])])
    with pytest.raises(ValueError, match="invalide"):
        admin(registry, bow_tie)
    assert registry.store.keys() == ()


def test_l_shape_and_multipolygon_are_registered(registry, tmp_path):
    l_path = write_geojson(tmp_path / "l.geojson", [utm(L_SHAPE)])
    multi = write_geojson(tmp_path / "m.geojson", [MultiPolygon([box(9.40, 0.40, 9.41, 0.41),
                                                                box(9.43, 0.40, 9.44, 0.41)])])
    l_record = registry.register(territory_id="import-l", label="Zone en L", kind=CUSTOM_IMPORT,
                                 legal_status=USER_PROVIDED, source=GBOPEN, geometry_path=l_path, now=NOW)
    m_record = registry.register(territory_id="import-m", label="Zone et île", kind=CUSTOM_IMPORT,
                                 legal_status=USER_PROVIDED, source=GBOPEN, geometry_path=multi, now=NOW)
    assert l_record.area_km2 == pytest.approx(0.75, rel=2e-3) and l_record.vertices == 7
    assert m_record.parts == 2


def test_historical_zones_keep_identifiers_and_identity(tmp_path):
    from src.config import DATA_DIR

    aoi_dir = DATA_DIR / "aoi"
    if not all((aoi_dir / f"{name}.geojson").exists() for name in ("akanda", "libreville", "owendo")):
        pytest.skip("Zones historiques absentes.")
    copy = tmp_path / "aoi"
    shutil.copytree(aoi_dir, copy)     # The real data directory is never written.
    registry = TerritoryRegistry(tmp_path / "data")
    records = register_historical_zones(registry, copy, now=NOW)
    assert [r.territory_id for r in records] == ["akanda", "libreville", "owendo"]
    for record in records:
        original = load_aoi_identity(aoi_dir / f"{record.territory_id}.geojson", record.territory_id)
        assert (record.geometry_sha256, record.file_sha256) == (original.geometry_sha256, original.file_sha256)
        assert record.kind == ACQUISITION_ZONE and record.legal_status == GEONAF_ZONE and record.admin is None
        assert "12 × 12 km" in record.label and record.scientific_profile_id
        assert registry.geometry_path(record).read_bytes() == (aoi_dir / f"{record.territory_id}.geojson").read_bytes()
    assert register_historical_zones(registry, copy, now=NOW) == records      # Idempotent.
    assert registry.get("akanda").area_km2 == pytest.approx(144.109, rel=1e-4)
