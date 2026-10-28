"""Resource guards: thresholds, overruns, unknown inputs, imports and calibration; offline."""

import sys

import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from src.services.resource_guards import (
    MIB, OK, REFUSED, UNKNOWN, WARNING, ResourceLimits, assess_import, assess_zone, available_memory_bytes,
    estimate_raster_cells, estimate_s2_tiles,
)
from tests.test_aoi_identity import write_geojson

LIMITS = ResourceLimits()


def utm_square(x0, y0, width_m, height_m, epsg=32632):
    return gpd.GeoSeries([box(x0, y0, x0 + width_m, y0 + height_m)], crs=f"EPSG:{epsg}").to_crs(4326).iloc[0]


AKANDA_LIKE = utm_square(537_000, 51_000, 12_000, 12_000)          # Same UTM square as the real zone.
DEPARTMENT_LIKE = utm_square(530_000, 30_000, 17_000, 22_000)       # ≈ Libreville department bbox (gbOpen).
LARGE = utm_square(500_000, 10_000, 50_000, 50_000)
PROVINCE_LIKE = utm_square(450_000, 0, 75_000, 132_000)             # ≈ Komo-Mondah (gbHumanitarian).


def test_akanda_like_zone_is_allowed_and_estimates_are_prudent():
    assessment = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_estimated_bytes=24_267_051, available_bytes=None)
    assert assessment.allowed and not assessment.warnings
    assert assessment.get("raster_cells").value == pytest.approx(1201 * 1201, rel=0.01)
    assert assessment.get("s2_tiles").value == 1
    # Measured process peaks (benchmark 23/10/2026): pair analysis 267 MiB, key figures with OSM 526 MiB.
    assert assessment.get("memory").value >= 526
    raster_only = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_wanted=False, available_bytes=None)
    assert raster_only.get("memory").value >= 267 and "osm_bbox" not in {c.name for c in raster_only.checks}


def test_department_sized_zone_is_allowed_with_a_prudent_memory_bound():
    assessment = assess_zone(DEPARTMENT_LIKE, limits=LIMITS, osm_wanted=False, available_bytes=None)
    cells = assessment.get("raster_cells").value
    assert 3.7e6 <= cells <= 3.8e6 and assessment.get("raster_cells").status == OK
    assert assessment.get("memory").value >= 336     # Measured: 336 MiB peak for 4.8 M cells (gbHumanitarian).
    assert assessment.allowed


def test_large_zone_is_refused_with_reasons():
    assessment = assess_zone(LARGE, limits=LIMITS, osm_estimated_bytes=10 * MIB, available_bytes=None)
    assert not assessment.allowed
    refused = {check.name for check in assessment.refusals}
    assert {"raster_cells", "memory"} <= refused
    assert all("limite" in check.reason for check in assessment.refusals)


def test_province_sized_zone_refuses_cells_tiles_and_single_osm_query():
    assessment = assess_zone(PROVINCE_LIKE, limits=LIMITS, osm_estimated_bytes=None, available_bytes=None)
    assert {"raster_cells", "osm_bbox"} <= {check.name for check in assessment.refusals}
    assert assessment.get("s2_tiles").value >= 2 and assessment.get("s2_tiles").status in (WARNING, REFUSED)
    assert "découpage en tuiles non disponible" in assessment.get("osm_bbox").reason


@pytest.mark.parametrize("value, status", [(4_000_000, OK), (4_000_001, WARNING), (16_000_000, WARNING),
                                           (16_000_001, REFUSED)])
def test_threshold_boundaries(value, status):
    from src.services.resource_guards import _threshold

    assert _threshold("c", "c", value, "cellules", LIMITS.warn_cells, LIMITS.max_cells, "N").status == status


def test_unknown_osm_volume_is_reported_not_assumed_small():
    assessment = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_estimated_bytes=None, available_bytes=None)
    assert assessment.allowed
    assert assessment.get("osm_bytes").status == UNKNOWN and assessment.get("memory").status == UNKNOWN
    assert {c.name for c in assessment.warnings} == {"osm_bytes", "memory"}


def test_osm_volume_over_the_ceiling_is_refused():
    assessment = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_estimated_bytes=70 * MIB, available_bytes=None)
    assert assessment.get("osm_bytes").status == REFUSED and not assessment.allowed


def test_memory_thresholds_follow_available_memory():
    # Akanda-like raster estimate ≈ 374 MiB: warning above half the free memory, refusal above 90 %.
    warned = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_wanted=False, available_bytes=600 * MIB).get("memory")
    assert (warned.warn_at, warned.refuse_at) == (pytest.approx(300), pytest.approx(540))
    assert warned.status == WARNING and "mémoire libre" in warned.reason
    refused = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_wanted=False, available_bytes=400 * MIB).get("memory")
    assert refused.status == REFUSED and refused.refuse_at == pytest.approx(360)


def test_akanda_multisource_stays_allowed_on_a_loaded_host():
    # Regression (23/10/2026): 1.08 GiB free on the development host must not block the existing analysis.
    memory = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_estimated_bytes=24_267_051,
                         available_bytes=int(1.08 * 1024 * MIB)).get("memory")
    assert memory.status != REFUSED


class _UnreadableMemory:
    @staticmethod
    def virtual_memory():
        raise PermissionError("/proc/meminfo")


@pytest.mark.parametrize("module", [None, _UnreadableMemory], ids=["psutil absent", "mémoire illisible"])
def test_unknown_free_memory_keeps_the_configured_ceilings(monkeypatch, module):
    monkeypatch.setitem(sys.modules, "psutil", module)   # None makes ``import psutil`` raise ImportError.
    assert available_memory_bytes() is None
    memory = assess_zone(AKANDA_LIKE, limits=LIMITS, osm_wanted=False).get("memory")   # Default reads the host.
    assert (memory.warn_at, memory.refuse_at) == (pytest.approx(1024), pytest.approx(2048))
    assert memory.status == OK and "mémoire libre" not in memory.reason


def test_panel_verdict_follows_free_memory_instead_of_a_stale_cache(monkeypatch):
    from src.components import resource_panel

    calls, free = [], {"bytes": 8 * 1024 * MIB}
    monkeypatch.setattr(resource_panel, "available_memory_bytes", lambda: free["bytes"])
    monkeypatch.setattr(resource_panel, "assess_zone", lambda *a, **kw: calls.append(kw) or assess_zone(*a, **kw))
    resource_panel._cached_assessment.clear()
    try:
        assess = lambda: resource_panel.assess_geometry(AKANDA_LIKE, "akanda-like").get("memory")
        assert assess().status == OK
        free["bytes"] = 600 * MIB          # Load rises: a new verdict, not the one cached above.
        warned = assess()
        assert warned.status == WARNING and calls[-1]["available_bytes"] == 576 * MIB   # Rounded down (64 MiB).
        free["bytes"] = 630 * MIB          # Same 64 MiB step: the cached verdict is reused.
        assert assess() == warned and len(calls) == 2
    finally:
        resource_panel._cached_assessment.clear()


def test_tiles_across_utm_zones_hemispheres_and_100km_lines():
    assert len(estimate_s2_tiles(box(9.40, 0.40, 9.45, 0.45))) == 1
    assert len(estimate_s2_tiles(box(11.95, 0.40, 12.05, 0.45))) == 2       # UTM zones 32 and 33.
    assert len(estimate_s2_tiles(box(9.40, -0.05, 9.45, 0.05))) == 2        # Equator: N and M latitude bands.
    assert len(estimate_s2_tiles(utm_square(590_000, 40_000, 20_000, 10_000))) == 2   # Easting 600 km line.
    assert {epsg for epsg, _, _ in estimate_s2_tiles(box(9.40, -0.05, 9.45, 0.05))} == {32632, 32732}


def test_cells_cover_the_bounding_box_not_only_the_polygon():
    triangle = gpd.GeoSeries([Polygon([(500_000, 0), (510_000, 0), (500_000, 10_000)])], crs=32632).to_crs(4326).iloc[0]
    _, _, cells = estimate_raster_cells(triangle, 10.0)
    assert cells >= 1000 * 1000      # The pipelines allocate the bounding box grid.


def test_empty_zone_is_refused():
    with pytest.raises(ValueError, match="vide"):
        assess_zone(Polygon(), limits=LIMITS, available_bytes=None)


def test_limits_are_configurable_and_validated():
    custom = ResourceLimits.from_environment({"GEONAF_LIMIT_MAX_CELLS": "1000000",
                                              "GEONAF_LIMIT_WARN_CELLS": "500000"})
    assert (custom.max_cells, custom.warn_cells) == (1_000_000, 500_000) and custom.fingerprint != LIMITS.fingerprint
    assert not assess_zone(AKANDA_LIKE, limits=custom, osm_wanted=False, available_bytes=None).allowed
    with pytest.raises(ValueError, match="non numérique"):
        ResourceLimits.from_environment({"GEONAF_LIMIT_MAX_CELLS": "beaucoup"})
    with pytest.raises(ValueError, match="supérieur au seuil de refus"):
        ResourceLimits(warn_cells=10, max_cells=5)
    with pytest.raises(ValueError, match="invalide"):
        ResourceLimits(bytes_per_cell=-1)


def test_import_size_is_checked_before_parsing(tmp_path):
    huge = tmp_path / "huge.geojson"
    huge.write_bytes(b"{" + b" " * (6 * MIB))     # Not even valid JSON: must not be parsed.
    assessment = assess_import(huge, limits=LIMITS)
    assert not assessment.allowed and [c.name for c in assessment.checks] == ["import_bytes"]


def test_import_vertices_and_parts_limits(tmp_path):
    small = ResourceLimits(max_import_vertices=10, max_import_parts=2)
    ok = write_geojson(tmp_path / "ok.geojson", [box(9.40, 0.40, 9.41, 0.41)])
    assert assess_import(ok, limits=small).allowed
    many_parts = write_geojson(tmp_path / "parts.geojson", [MultiPolygon(
        [box(9.40 + i * 0.01, 0.40, 9.405 + i * 0.01, 0.405) for i in range(3)])])
    refused = assess_import(many_parts, limits=small)
    assert {c.name for c in refused.refusals} == {"import_vertices", "import_parts"}


def test_import_missing_or_invalid_file(tmp_path):
    with pytest.raises(ValueError, match="introuvable"):
        assess_import(tmp_path / "absent.geojson", limits=LIMITS)
    bow_tie = write_geojson(tmp_path / "bad.geojson", [Polygon([(9.3, 0.4), (9.4, 0.5), (9.4, 0.4), (9.3, 0.5)])])
    with pytest.raises(ValueError, match="invalide"):
        assess_import(bow_tie, limits=LIMITS)
