"""Comparison of two representations of a territory; offline."""

from datetime import datetime, timezone

import pytest
from pyproj import Geod
from shapely.geometry import LineString, MultiPolygon, Polygon, box

from src.data.territory_registry import (
    ADMIN_UNIT, COMMUNITY, HUMANITARIAN, AdminInfo, TerritoryRegistry, TerritorySource,
)
from src.services.territory_comparison import compare_geometries, compare_territories, match_by_normalized_name
from tests.test_aoi_identity import write_geojson

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
SQUARE = box(9.40, 0.40, 9.50, 0.50)


def test_identical_geometries():
    result = compare_geometries(SQUARE, SQUARE)
    assert result.overlap_percent == pytest.approx(100) and result.symmetric_difference_km2 == pytest.approx(0, abs=1e-9)
    assert result.relative_area_difference_percent == pytest.approx(0)
    assert "pas un changement" in result.nature and "EPSG:6933" in result.method


def test_areas_are_true_ellipsoidal_areas():
    geodesic = abs(Geod(ellps="WGS84").geometry_area_perimeter(SQUARE)[0]) / 1e6
    assert compare_geometries(SQUARE, SQUARE).area_a_km2 == pytest.approx(geodesic, rel=1e-4)


def test_partially_overlapping_geometries():
    shifted = box(9.45, 0.40, 9.55, 0.50)
    result = compare_geometries(SQUARE, shifted, label_a="gbOpen", label_b="gbHumanitarian")
    assert result.a_covered_by_b_percent == pytest.approx(50, rel=1e-3)
    assert result.overlap_percent == pytest.approx(100 / 3, rel=1e-3)          # Jaccard: 1 / 3.
    assert result.symmetric_difference_km2 == pytest.approx(result.area_a_km2, rel=1e-3)
    larger = compare_geometries(SQUARE, box(9.40, 0.40, 9.60, 0.50))
    assert larger.relative_area_difference_percent == pytest.approx(100, rel=1e-3)
    assert larger.a_covered_by_b_percent == pytest.approx(100, rel=1e-6)


def test_disjoint_geometries():
    result = compare_geometries(SQUARE, box(10.0, 0.40, 10.1, 0.50))
    assert result.intersection_km2 == 0 and result.overlap_percent == 0
    assert result.symmetric_difference_km2 == pytest.approx(result.area_a_km2 + result.area_b_km2)


def test_multipolygon_is_supported():
    islands = MultiPolygon([box(9.40, 0.40, 9.45, 0.50), box(9.46, 0.40, 9.50, 0.50)])
    result = compare_geometries(SQUARE, islands)
    assert result.b_covered_by_a_percent == pytest.approx(100, rel=1e-6) and result.overlap_percent == pytest.approx(90, rel=1e-3)


@pytest.mark.parametrize("geometry, message", [
    (Polygon([(9.0, 0.0), (9.1, 0.1), (9.1, 0.0), (9.0, 0.1)]), "invalide"),
    (LineString([(9.0, 0.0), (9.1, 0.1)]), "non surfacique"),
    (Polygon(), "vide"),
])
def test_invalid_geometries_are_refused(geometry, message):
    with pytest.raises(ValueError, match=message):
        compare_geometries(SQUARE, geometry, label_b="B")


def _register(registry, tmp_path, territory_id, geometry, status, year, source_id):
    path = write_geojson(tmp_path / f"{territory_id}.geojson", [geometry])
    return registry.register(
        territory_id=territory_id, label="Libreville", kind=ADMIN_UNIT, legal_status=status,
        source=TerritorySource(source_id, f"{source_id} GAB ADM2", "2023-12-12", "CC-BY-3.0", "geoBoundaries",
                               reference_year=year),
        geometry_path=path, admin=AdminInfo("GAB", "ADM2", "Department"), now=NOW)


def test_registered_referentials_are_compared_without_being_merged(tmp_path):
    registry = TerritoryRegistry(tmp_path)
    a = _register(registry, tmp_path, "gab-gbopen-adm2-libreville", SQUARE, COMMUNITY, 2018, "geoboundaries-gbopen")
    b = _register(registry, tmp_path, "gab-gbhumanitarian-adm2-libreville", box(9.40, 0.40, 9.55, 0.50),
                  HUMANITARIAN, 2015, "geoboundaries-gbhumanitarian")
    result = compare_territories(registry, a.territory_id, b.territory_id)
    assert result.relative_area_difference_percent == pytest.approx(50, rel=1e-3)
    assert any("Millésimes différents" in note for note in result.notes)
    assert any("référentiel communautaire" in note for note in result.notes)
    assert registry.versions(a.territory_id) == (a,) and registry.versions(b.territory_id) == (b,)
    with pytest.raises(ValueError, match="elle-même"):
        compare_territories(registry, a.territory_id, a.territory_id)


def test_versions_of_one_territory_are_not_a_real_change(tmp_path):
    registry = TerritoryRegistry(tmp_path)
    first = _register(registry, tmp_path, "gab-gbopen-adm2-libreville", SQUARE, COMMUNITY, 2018, "geoboundaries-gbopen")
    (tmp_path / "v2").mkdir()
    second = _register(registry, tmp_path / "v2", "gab-gbopen-adm2-libreville", box(9.40, 0.40, 9.52, 0.50),
                       COMMUNITY, 2018, "geoboundaries-gbopen")
    result = compare_territories(registry, first.territory_id, second.territory_id, version_a=1, version_b=2)
    assert any("pas un changement réel" in note for note in result.notes)


def test_name_matching_ignores_accents_and_skips_ambiguity(tmp_path):
    class R:
        def __init__(self, label):
            self.label = label

    a = [R("Lèbombi-Leyou"), R("Noya"), R("Komo-Océan")]
    b = [R("Lebombi-Leyou"), R("Noya"), R("Noya")]
    pairs = match_by_normalized_name(a, b)
    assert [(x.label, y.label) for x, y in pairs] == [("Lèbombi-Leyou", "Lebombi-Leyou")]
