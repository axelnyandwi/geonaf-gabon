"""Territorial key figures on synthetic layers; offline, exact expected values."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from src.data.openstreetmap import OpenStreetMapAdapter, load_osm_snapshot
from src.data.worldcover import WorldCoverAdapter
from src.services.external_layers import open_store
from src.services.territorial_indicators import (
    AFRICA_DEFAULT_V1, AVAILABLE, LOW, MEDIUM, NOT_ASSESSED, UNAVAILABLE, assess_change, build_indicators,
    building_figures, compute_territorial_indicators, indicator_inputs, landcover_areas, road_lengths,
)

X0, Y0 = 500000.0, 100000.0     # UTM 32N, near the equator (Gabon).
RETRIEVED = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SNAPSHOT = "2026-10-22T16:57:05Z"


def write_aoi(path, x0=X0, y0=Y0, size=1000.0):
    gpd.GeoDataFrame({"name": ["test"]}, geometry=[box(x0, y0, x0 + size, y0 + size)],
                     crs="EPSG:32632").to_crs("EPSG:4326").to_file(path)
    return path


def landcover_codes():
    """100 × 100 cells of 10 m: left half tree cover, top-right water, bottom-right split built-up/mangrove."""
    codes = np.full((100, 100), 10, dtype="uint8")
    codes[:50, 50:] = 80
    codes[50:, 50:75] = 50
    codes[50:, 75:] = 95
    return codes


def write_landcover(path, codes, *, origin=(X0, Y0 + 1000.0), crs="EPSG:32632", res=10.0):
    with rasterio.open(path, "w", driver="GTiff", width=codes.shape[1], height=codes.shape[0], count=1,
                       dtype="uint8", crs=crs, transform=from_origin(*origin, res, res), nodata=0) as dst:
        dst.write(codes, 1)
    return path


def worldcover_layer(path, version="v200", aoi_id="test"):
    adapter = WorldCoverAdapter(version)
    return path, adapter.describe_local(path, retrieved_at=RETRIEVED, extra_provenance={"aoi_id": aoi_id})


def utm_to_wgs84(geometries):
    return gpd.GeoSeries(geometries, crs="EPSG:32632").to_crs("EPSG:4326")


def osm_frame(rows):
    """rows: (category, detail, UTM geometry)."""
    return gpd.GeoDataFrame({
        "osm_id": [f"way/{i}" for i in range(len(rows))], "category": [r[0] for r in rows],
        "group": ["g"] * len(rows), "detail": [r[1] for r in rows]},
        geometry=utm_to_wgs84([r[2] for r in rows]).values, crs="EPSG:4326")


def osm_descriptor(bounds, snapshot=SNAPSHOT):
    payload = {"osm3s": {"timestamp_osm_base": snapshot}, "elements": []}
    return OpenStreetMapAdapter().parse_overpass(payload, retrieved_at=RETRIEVED, wgs84_bounds=bounds)[1]


def aoi_bounds(path):
    return tuple(float(v) for v in gpd.read_file(path).to_crs("EPSG:4326").total_bounds)


@pytest.fixture
def aoi(tmp_path):
    return write_aoi(tmp_path / "test.geojson")


def test_landcover_areas_use_every_classified_cell_with_exact_weights(tmp_path, aoi):
    areas, classified, total = landcover_areas([worldcover_layer(write_landcover(tmp_path / "wc.tif",
                                                                                 landcover_codes()))], aoi)
    assert classified == pytest.approx(total, rel=1e-6)  # No NDVI or cloud mask: the whole AOI counts.
    shares = {code: area / total for code, area in areas.items()}
    assert shares == pytest.approx({10: 0.5, 80: 0.25, 50: 0.125, 95: 0.125}, abs=1e-6)
    assert total == pytest.approx(1e6, rel=2e-3)   # UTM vs equal-area scale factor near the central meridian.


def test_geographic_landcover_grid_weights_cover_the_aoi(tmp_path, aoi):
    left, bottom, right, top = aoi_bounds(aoi)
    res = 1 / 12000
    width, height = int(np.ceil((right - left) / res)) + 2, int(np.ceil((top - bottom) / res)) + 2
    path = write_landcover(tmp_path / "wc4326.tif", np.full((height, width), 30, dtype="uint8"),
                           origin=(left - res, top + res), crs="EPSG:4326", res=res)
    areas, classified, total = landcover_areas([worldcover_layer(path)], aoi)
    assert classified == pytest.approx(total, rel=1e-6) and set(areas) == {30}


def test_landcover_indicators_groups_shares_and_dates(tmp_path, aoi):
    result = build_indicators(aoi_id="test", aoi_path=aoi,
                              worldcover=[worldcover_layer(write_landcover(tmp_path / "wc.tif", landcover_codes()))])
    land, vegetation, built = (result.get(i) for i in ("landcover.land_km2", "landcover.vegetation_km2",
                                                       "landcover.builtup_km2"))
    aoi_km2 = result.aoi_area_km2
    assert land.value == pytest.approx(0.75 * aoi_km2, rel=1e-6)
    assert vegetation.value == pytest.approx(0.625 * aoi_km2, rel=1e-6)
    assert dict(vegetation.shares) == pytest.approx({"zone d'étude": 62.5, "surface terrestre": 62.5 / 0.75})
    groups = {item.label: item.value for item in vegetation.breakdown}
    assert groups == pytest.approx({"Végétation naturelle": 0.5 * aoi_km2, "Cultures": 0.0,
                                    "Mangroves": 0.125 * aoi_km2}, rel=1e-6, abs=1e-12)
    assert built.value == pytest.approx(0.125 * aoi_km2, rel=1e-6)
    assert vegetation.reliability == MEDIUM and vegetation.source.nature == "Classification externe"
    dates = vegetation.dates
    assert (dates.observation_start_utc.year, dates.observation_end_utc.year) == (2021, 2021)
    assert dates.ingested_at_utc == RETRIEVED and "v200" in dates.reference_label


def test_partial_landcover_coverage_is_flagged_not_extrapolated(tmp_path, aoi):
    codes = landcover_codes()
    codes[:, :10] = 0   # 10 % of the AOI without class.
    result = build_indicators(aoi_id="test", aoi_path=aoi,
                              worldcover=[worldcover_layer(write_landcover(tmp_path / "wc.tif", codes))])
    vegetation = result.get("landcover.vegetation_km2")
    assert vegetation.coverage_percent == pytest.approx(90.0, abs=1e-3)
    assert vegetation.reliability == LOW and result.warnings
    assert vegetation.value == pytest.approx(0.525 * result.aoi_area_km2, rel=1e-6)


def test_landcover_versions_cannot_be_mixed(tmp_path, aoi):
    codes = landcover_codes()
    tiles = [worldcover_layer(write_landcover(tmp_path / "a.tif", codes), "v100"),
             worldcover_layer(write_landcover(tmp_path / "b.tif", codes), "v200")]
    with pytest.raises(ValueError, match="versions différentes"):
        build_indicators(aoi_id="test", aoi_path=aoi, worldcover=tiles)


def test_road_lengths_are_clipped_grouped_and_exclude_unusable_ways(aoi):
    y = Y0 + 500
    frame = osm_frame([
        ("highway", "residential", LineString([(X0 - 500, y), (X0 + 1500, y)])),   # 1 000 m inside.
        ("highway", "primary", LineString([(X0 + 100, Y0 + 100), (X0 + 100, Y0 + 400)])),
        ("highway", "primary", LineString([(X0 + 110, Y0 + 100), (X0 + 110, Y0 + 400)])),  # Second carriageway.
        ("highway", "proposed", LineString([(X0 + 200, Y0 + 100), (X0 + 200, Y0 + 900)])),
        ("highway", "bus_guideway", LineString([(X0 + 300, Y0 + 100), (X0 + 300, Y0 + 200)])),
        ("highway", "track", LineString([(X0 + 2000, Y0), (X0 + 2500, Y0)])),        # Outside.
        ("building", "yes", box(X0 + 10, Y0 + 10, X0 + 20, Y0 + 20)),
    ])
    total, categories, excluded, areas = road_lengths(frame, aoi)
    by_label = {label: km for label, km, _ in categories}
    assert by_label == pytest.approx({"Axes principaux": 0.6, "Voies de desserte": 1.0, "Autres voies": 0.1},
                                     abs=1e-3)
    assert total == pytest.approx(1.7, abs=1e-3) and excluded == pytest.approx(0.8, abs=1e-3) and areas == 0


def test_buildings_counted_once_across_adjacent_study_zones(tmp_path):
    west = write_aoi(tmp_path / "west.geojson")
    east = write_aoi(tmp_path / "east.geojson", x0=X0 + 1000)
    frame = osm_frame([
        ("building", "yes", box(X0 + 980, Y0 + 500, X0 + 1010, Y0 + 520)),    # Mostly west.
        ("building", "house", box(X0 + 100, Y0 + 100, X0 + 120, Y0 + 110)),
        ("highway", "service", LineString([(X0, Y0), (X0 + 10, Y0 + 10)])),
    ])
    west_count, west_footprint, west_straddling = building_figures(frame, west)
    east_count, east_footprint, _ = building_figures(frame, east)
    assert (west_count, east_count, west_straddling) == (2, 0, 1)
    assert west_footprint + east_footprint == pytest.approx((30 * 20 + 20 * 10) / 1e6, rel=1e-3)


def test_osm_indicators_wording_densities_and_dates(tmp_path, aoi):
    frame = osm_frame([("building", "yes", box(X0 + 10 * i, Y0 + 10, X0 + 10 * i + 5, Y0 + 15)) for i in range(10)])
    result = build_indicators(aoi_id="test", aoi_path=aoi, osm=(frame, osm_descriptor(aoi_bounds(aoi))),
                              worldcover=[worldcover_layer(write_landcover(tmp_path / "wc.tif", landcover_codes()))])
    count, land_density, aoi_density = (result.get(i) for i in (
        "osm.buildings_count", "osm.buildings_density_land", "osm.buildings_density_aoi"))
    assert count.value == 10 and "cartographiés" in count.label and count.reliability == NOT_ASSESSED
    assert any("pas le nombre total réel" in limit for limit in count.limits)
    assert land_density.value == pytest.approx(10 / (0.75 * result.aoi_area_km2), rel=1e-6)
    assert aoi_density.value == pytest.approx(10 / result.aoi_area_km2, rel=1e-6)
    dates = count.dates
    assert dates.observation_start_utc is None and "inconnue" in dates.observation_note
    assert dates.reference_at_utc == datetime(2026, 10, 22, 16, 57, 5, tzinfo=timezone.utc)
    assert dates.ingested_at_utc == RETRIEVED
    roads = result.get("osm.roads_km")
    assert roads.value == 0 and roads.status == AVAILABLE   # Nothing mapped ≠ source missing.
    assert any("chaussées séparées" in limit for limit in roads.limits)


def test_missing_sources_yield_no_value(aoi):
    result = build_indicators(aoi_id="test", aoi_path=aoi)
    assert set(result.missing) == {"ESA WorldCover", "OpenStreetMap"}
    unavailable = [item for item in result.indicators if item.status == UNAVAILABLE]
    assert len(unavailable) == 8 and all(item.value is None and item.source is None for item in unavailable)
    assert result.get("aoi.area_km2").value == pytest.approx(1.0, rel=2e-3)


def test_density_on_land_unavailable_without_landcover(aoi):
    result = build_indicators(aoi_id="test", aoi_path=aoi, osm=(osm_frame([]), osm_descriptor(aoi_bounds(aoi))))
    assert result.get("osm.buildings_density_land").value is None
    assert result.get("osm.buildings_density_aoi").value == 0


def test_every_available_indicator_documents_its_contract(tmp_path, aoi):
    frame = osm_frame([("highway", "tertiary", LineString([(X0 + 1, Y0 + 1), (X0 + 900, Y0 + 1)]))])
    result = build_indicators(aoi_id="test", aoi_path=aoi, osm=(frame, osm_descriptor(aoi_bounds(aoi))),
                              worldcover=[worldcover_layer(write_landcover(tmp_path / "wc.tif", landcover_codes()))])
    for item in result.indicators:
        assert item.status == AVAILABLE and item.value is not None and np.isfinite(item.value)
        assert item.unit and item.method and item.reliability and item.reliability_reasons and item.source
        assert item.aoi_id == "test" and item.aoi_sha256 == result.aoi_sha256
        assert item.profile_fingerprint == AFRICA_DEFAULT_V1.fingerprint
        if item.indicator_id != "aoi.area_km2":
            assert item.dates is not None and item.dates.reference_label and item.limits


def _indicator(tmp_path, aoi, version="v200", name="wc.tif"):
    return build_indicators(aoi_id="test", aoi_path=aoi, worldcover=[worldcover_layer(
        write_landcover(tmp_path / name, landcover_codes()), version)]).get("landcover.vegetation_km2")


def test_no_change_from_a_single_reference(tmp_path, aoi):
    item = _indicator(tmp_path, aoi)
    change = assess_change(item, item)
    assert not change.comparable and change.delta is None
    assert any("Même référentiel" in reason for reason in change.reasons)


def test_landcover_versions_are_not_a_change(tmp_path, aoi):
    change = assess_change(_indicator(tmp_path, aoi, "v100", "a.tif"), _indicator(tmp_path, aoi, "v200", "b.tif"))
    assert not change.comparable and any("algorithmes différents" in reason for reason in change.reasons)


def test_osm_snapshots_compare_as_mapping_change_only(aoi):
    bounds = aoi_bounds(aoi)
    def count(n, snapshot):
        frame = osm_frame([("building", "yes", box(X0 + 10 * i, Y0 + 10, X0 + 10 * i + 5, Y0 + 15))
                           for i in range(n)])
        return build_indicators(aoi_id="test", aoi_path=aoi,
                                osm=(frame, osm_descriptor(bounds, snapshot))).get("osm.buildings_count")
    before, after = count(3, "2025-01-01T00:00:00Z"), count(5, "2026-01-01T00:00:00Z")
    change = assess_change(before, after)
    assert change.comparable and change.delta == 2
    assert "cartographie" in change.kind and "ni nouvelles constructions" in change.kind
    assert not assess_change(after, before).comparable   # Reversed chronology.


def test_change_refused_across_zones_and_profiles(tmp_path, aoi):
    item = _indicator(tmp_path, aoi)
    other_zone = replace(item, aoi_sha256="0" * 64)
    other_profile = replace(item, profile_fingerprint="1" * 64)
    assert any("Zones" in r for r in assess_change(item, other_zone).reasons)
    assert any("Profils" in r for r in assess_change(item, other_profile).reasons)


def test_profile_fingerprint_tracks_groupings():
    changed = replace(AFRICA_DEFAULT_V1, vegetation_groups=(("Végétation naturelle", (10, 20, 30, 90)),))
    assert changed.fingerprint != AFRICA_DEFAULT_V1.fingerprint
    assert set(AFRICA_DEFAULT_V1.vegetation_classes) == {10, 20, 30, 40, 90, 95}


def store_layers(data_dir, aoi_id="test", *, osm=True, worldcover=True):
    """Publish layers through the real store and catalogue, as the retrieval services do.

    An existing square AOI is kept (e.g. the Sentinel-2 test zone) and the layers follow its extent.
    """
    (data_dir / "aoi").mkdir(parents=True, exist_ok=True)
    aoi = data_dir / "aoi" / f"{aoi_id}.geojson"
    if not aoi.exists():
        write_aoi(aoi)
    minx, miny, maxx, maxy = gpd.read_file(aoi).to_crs("EPSG:32632").total_bounds
    size = float(maxx - minx)
    store, repository = open_store(data_dir)
    if worldcover:
        source = write_landcover(data_dir / "source.tif", landcover_codes(), origin=(minx, maxy), res=size / 100)
        key = f"worldcover/ESA_WorldCover_10m_2021_v200_N00E009_Map_{aoi_id}.tif"
        path = store.write_bytes(key, source.read_bytes())
        source.unlink()
        repository.save(worldcover_layer(path, aoi_id=aoi_id)[1], key)
    if osm:
        bounds = aoi_bounds(aoi)
        middle = (miny + maxy) / 2
        lines = gpd.GeoSeries([
            LineString([(minx + 0.1 * size, middle), (minx + 0.6 * size, middle)]),
            box(minx + 0.01 * size, miny + 0.01 * size, minx + 0.02 * size, miny + 0.02 * size).exterior,
        ], crs="EPSG:32632").to_crs(4326)
        elements = [{"type": "way", "id": i + 1, "tags": tags,
                     "geometry": [{"lon": x, "lat": y} for x, y in line.coords]}
                    for i, (line, tags) in enumerate(zip(lines, ({"highway": "residential"}, {"building": "yes"})))]
        payload = {"osm3s": {"timestamp_osm_base": SNAPSHOT}, "elements": elements}
        raw = json.dumps(payload).encode()
        wrapper = {"geonaf": {"query_sha256": "q" * 64, "response_sha256": hashlib.sha256(raw).hexdigest(),
                              "response_bytes": len(raw), "retrieved_at_utc": RETRIEVED.isoformat()},
                   "response": payload}
        key = "openstreetmap/test/20261022T170000Z.json"
        store.write_bytes(key, json.dumps(wrapper).encode())
        repository.save(load_osm_snapshot(store, key, bounds)[1], key)
    return aoi


def test_end_to_end_from_stored_layers_offline(tmp_path):
    store_layers(tmp_path)
    inputs = indicator_inputs(tmp_path, "test")
    assert inputs.osm is not None and len(inputs.worldcover) == 1 and len(inputs.temporal_notes) == 2
    result = compute_territorial_indicators(inputs, aoi_label="Zone d'étude Test")
    assert result.aoi_label == "Zone d'étude Test" and not result.missing
    assert result.get("osm.roads_km").value == pytest.approx(0.5, abs=1e-3)
    assert result.get("osm.buildings_count").value == 1
    assert result.get("landcover.vegetation_km2").value == pytest.approx(0.625 * result.aoi_area_km2, rel=1e-6)
    assert indicator_inputs(tmp_path, "test").key == inputs.key   # Stable cache key.


def test_layers_of_an_adjacent_zone_are_not_used(tmp_path):
    store_layers(tmp_path, "other", osm=False)
    write_aoi(tmp_path / "aoi" / "test.geojson")
    inputs = indicator_inputs(tmp_path, "test")
    assert inputs.worldcover == ()
