"""Multisource contracts and joins; offline, synthetic layers only."""

from datetime import datetime, timezone
import socket

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from src.data.external_sources import (
    ExternalLayerDescriptor, InMemoryLayerRepository, LayerQuality, SpatialExtent, TemporalValidity,
    assess_temporal_join,
)
from src.data.openstreetmap import LICENSE as OSM_LICENSE, OpenStreetMapAdapter
from src.data.worldcover import CLASSES, WorldCoverAdapter
from src.services.local_preview import analyze_local_akanda, list_local_observations
from src.services.multisource import join_categorical_raster, join_vector_categories
from tests.test_local_preview import local_data

UTC = timezone.utc
AKANDA = (9.388, 0.354, 9.496, 0.463)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_temporal_join_reports_offsets():
    year = TemporalValidity("interval", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 12, 31, tzinfo=UTC))
    assert assess_temporal_join(year, datetime(2021, 6, 1, tzinfo=UTC), 0).relation == "within"
    later = assess_temporal_join(year, datetime(2025, 1, 25, tzinfo=UTC), 365)
    assert later.relation == "after_layer" and later.offset_days == pytest.approx(1121)
    assert not later.within_tolerance and "prudence" in later.message
    assert assess_temporal_join(year, datetime(2020, 12, 1, tzinfo=UTC), 31).within_tolerance
    with pytest.raises(ValueError):
        TemporalValidity("snapshot", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 1, 2, tzinfo=UTC))
    with pytest.raises(ValueError):
        assess_temporal_join(year, datetime(2021, 6, 1), 1)  # Naive datetime.


def test_worldcover_plans_tiles_without_network():
    adapter = WorldCoverAdapter()
    assert adapter.tiles(AKANDA) == ("N00E009",)
    assert adapter.tiles((9.5, -0.5, 12.5, 0.5)) == ("S03E009", "S03E012", "N00E009", "N00E012")
    plan = adapter.plan(AKANDA)
    assert plan.resources == ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
                              "ESA_WorldCover_10m_2021_v200_N00E009_Map.tif",)
    assert any("fenêtrée" in note for note in plan.notes)
    with pytest.raises(ValueError):
        adapter.tiles((0, 0, 20, 20))
    with pytest.raises(ValueError):
        WorldCoverAdapter("v999")


def test_osm_query_is_bounded_and_parsing_keeps_snapshot_provenance():
    adapter = OpenStreetMapAdapter()
    query = adapter.overpass_query(AKANDA)
    assert '(0.354000,9.388000,0.463000,9.496000)' in query and "out geom;" in query
    with pytest.raises(ValueError):
        adapter.overpass_query(AKANDA, {'building"];out;': None})
    with pytest.raises(ValueError):
        adapter.overpass_query((9, 0, 11, 2))
    payload = {"osm3s": {"timestamp_osm_base": "2026-10-20T08:00:00Z"}, "elements": [
        {"type": "way", "id": 1, "tags": {"building": "yes"}, "geometry": [
            {"lat": 0.40, "lon": 9.40}, {"lat": 0.40, "lon": 9.41}, {"lat": 0.41, "lon": 9.41},
            {"lat": 0.40, "lon": 9.40}]},
        {"type": "way", "id": 2, "tags": {"highway": "primary"}, "geometry": [
            {"lat": 0.42, "lon": 9.42}, {"lat": 0.43, "lon": 9.43}]},
        {"type": "node", "id": 3, "lat": 0.4, "lon": 9.4, "tags": {"natural": "tree"}},
    ]}
    frame, descriptor = adapter.parse_overpass(payload, retrieved_at=datetime(2026, 10, 22, tzinfo=UTC),
                                               wgs84_bounds=AKANDA)
    assert list(frame["category"]) == ["building", "highway"]
    assert frame.geometry.iloc[0].geom_type == "Polygon" and frame.geometry.iloc[1].geom_type == "LineString"
    assert descriptor.temporal.kind == "snapshot" and descriptor.version == "20261020T080000Z"
    assert descriptor.license.spdx_id == "ODbL-1.0" and descriptor.license.share_alike
    assert descriptor.provenance["skipped_elements"] == "1"
    with pytest.raises(ValueError, match="Horodatage"):
        adapter.parse_overpass({"elements": []}, retrieved_at=datetime.now(UTC), wgs84_bounds=AKANDA)


def test_descriptor_rejects_local_paths_and_repository_orders_by_time(tmp_path):
    descriptor = _worldcover(tmp_path, "EPSG:32632")[1]
    with pytest.raises(ValueError, match="chemin"):
        ExternalLayerDescriptor(**{**descriptor.__dict__, "provenance": {"file": str(tmp_path)}})
    with pytest.raises(ValueError):
        ExternalLayerDescriptor(**{**descriptor.__dict__, "kind": "vector"})
    repository = InMemoryLayerRepository()
    older = ExternalLayerDescriptor(**{**descriptor.__dict__, "version": "v100", "temporal": TemporalValidity(
        "interval", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))})
    repository.save(older)
    repository.save(descriptor)
    found = repository.find("esa-worldcover", descriptor.extent.wgs84_bounds, datetime(2025, 1, 25, tzinfo=UTC))
    assert [item.version for item in found] == ["v200", "v100"]
    assert not repository.find("esa-worldcover", (-10, -10, -9, -9))


def _worldcover(tmp_path, crs):
    """2×2 land cover over the local_data grid (EPSG:32632, origin 500000/100000, 10 m)."""
    path = tmp_path / f"worldcover-{crs.replace(':', '')}.tif"
    classes = np.array([[10, 95], [80, 10]], dtype="uint8")
    if crs == "EPSG:32632":
        transform, data = from_origin(500000, 100000, 10, 10), classes
    else:
        from rasterio.warp import transform_bounds
        left, bottom, right, top = transform_bounds("EPSG:32632", "EPSG:4326", 500000, 99980, 500020, 100000)
        size = 40  # Finer than 10 m so nearest sampling keeps each S2 cell's class.
        transform = rasterio.transform.from_bounds(left, bottom, right, top, size, size)
        data = np.kron(classes, np.ones((size // 2, size // 2), dtype="uint8"))
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                       dtype="uint8", crs=crs, transform=transform, nodata=0) as dst:
        dst.write(data, 1)
    return path, WorldCoverAdapter().describe_local(path, retrieved_at=datetime(2026, 10, 22, tzinfo=UTC))


@pytest.mark.parametrize("crs", ["EPSG:32632", "EPSG:4326"])
def test_categorical_join_on_pair_result(local_data, tmp_path, crs):
    earlier, later = list_local_observations(local_data)
    pair = analyze_local_akanda(earlier, later, local_data)
    path, descriptor = _worldcover(tmp_path, crs)
    assert descriptor.quality.status == "declared" and descriptor.quality.checksum_sha256
    joined = join_categorical_raster(
        pair.common_grid, pair.comparison.ndvi_date_2, pair.comparison.valid_date_2,
        observed_at=pair.later.acquired_at_utc, layer_path=path, descriptor=descriptor,
        aoi_path=local_data / "aoi/akanda.geojson")
    stats = {zone.code: zone for zone in joined.zones}
    # Valid S2 cells (SCL 4 and 5) are top-right (95) and bottom-left (80).
    assert stats[95].pixel_count == stats[80].pixel_count == 1 and stats[10].pixel_count == 0
    assert stats[95].mean == pytest.approx(0.6) and stats[95].area_m2 == pytest.approx(100, rel=1e-3)
    assert joined.layer_coverage_pixels == 2 and joined.unmatched_pixels == 0
    assert joined.temporal.relation == "after_layer" and not joined.temporal.within_tolerance
    assert "WorldCover" in joined.attribution
    assert set(CLASSES) == {zone.code for zone in joined.zones}


def test_vector_join_and_unmatched_cells(local_data):
    earlier, later = list_local_observations(local_data)
    pair = analyze_local_akanda(earlier, later, local_data)
    frame = gpd.GeoDataFrame({"category": ["building"]}, geometry=[box(500010, 99990, 500020, 100000)],
                             crs="EPSG:32632").to_crs("EPSG:4326")
    snapshot = datetime(2026, 10, 20, tzinfo=UTC)
    descriptor = ExternalLayerDescriptor(
        "openstreetmap", "OpenStreetMap", "20261020T000000Z", "vector",
        TemporalValidity("snapshot", snapshot, snapshot), None, "EPSG:4326",
        SpatialExtent("EPSG:4326", AKANDA, AKANDA), OSM_LICENSE, quality=LayerQuality("unknown"))
    joined = join_vector_categories(
        pair.common_grid, pair.comparison.ndvi_date_2, pair.comparison.valid_date_2,
        observed_at=pair.later.acquired_at_utc, frame=frame, descriptor=descriptor,
        aoi_path=local_data / "aoi/akanda.geojson")
    assert [(zone.label, zone.pixel_count) for zone in joined.zones] == [("building", 1)]
    assert joined.layer_coverage_pixels == 1 and joined.unmatched_pixels == 1
    assert "OpenStreetMap" in joined.attribution
