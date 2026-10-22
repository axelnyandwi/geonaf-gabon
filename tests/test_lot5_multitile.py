"""Lot 5: synthetic multi-tile mosaics, analysis modes and acquisition grouping; offline.

Tiles are tiny synthetic rasters with known values, so every expected count,
overlap and area-weighted statistic can be derived by hand.
"""

from datetime import datetime, timedelta, timezone
import io
import json
import socket

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import box

from src.data.acquisition import create_sentinel2_acquisition
from src.data.external_http import ExternalResponse
from src.data.layer_store import FileLayerRepository, LayerFileStore
from src.data.worldcover import WorldCoverAdapter, fetch_worldcover_window, fetch_worldcover_windows
from src.geo.mosaic import AlignedTile, aoi_grid, mosaic_tiles
from src.geo.scientific_profile import (
    ADVANCED_SCL_POLICIES, GABON_EQUATORIAL_EXPERIMENTAL as BASE, REFERENCE_PROFILES, advanced_configuration,
    reference_profile, standard_configuration,
)
from src.services.ndvi_observation import analyze_ndvi_mosaic, validate_acquisition_group
from tests.test_external_retrieval_core import write_cog
from tests.test_local_preview import local_data  # noqa: F401  (fixture)

UTC = timezone.utc
T0 = datetime(2025, 1, 25, 9, 31, 59, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


# --- Generic mosaic rules ---------------------------------------------------------------------------------

def tile(tile_id, values, valid, native=True):
    return AlignedTile(tile_id, "EPSG:32632", (10.0, 10.0), native, np.asarray(values), np.asarray(valid, bool))


def test_first_valid_tile_wins_overlaps_are_measured_and_gaps_stay_empty():
    a = tile("B", [[1.0, 2.0, 0.0]], [[True, True, False]])
    b = tile("A", [[9.0, 2.5, 0.0]], [[False, True, False]], native=False)
    c = tile("C", [[5.0, 5.0, 5.0]], [[True, False, False]])
    mosaic = mosaic_tiles([a, b, c], categorical=False)
    # Native tiles first (B, C), then A; the third cell is valid nowhere and stays empty.
    assert [t.tile_id for t in mosaic.contributions] == ["B", "C", "A"]
    np.testing.assert_array_equal(mosaic.valid, [[True, True, False]])
    assert mosaic.values[0, 0] == 1.0 and mosaic.values[0, 1] == 2.0 and np.isnan(mosaic.values[0, 2])
    assert mosaic.source.tolist() == [[0, 0, -1]]
    assert mosaic.overlap_cells == 2 and mosaic.conflicting_cells == 2 and mosaic.max_abs_difference == 4.0
    assert [(t.selected_cells, t.overlap_cells) for t in mosaic.contributions] == [(2, 0), (0, 1), (0, 1)]
    categorical = mosaic_tiles([tile("X", [[10, 50]], [[True, True]]), tile("Y", [[10, 95]], [[True, True]])],
                               categorical=True)
    assert categorical.conflicting_cells == 1 and categorical.values.tolist() == [[10, 50]]
    with pytest.raises(ValueError):
        mosaic_tiles([a, tile("D", [[1.0]], [[True]])], categorical=False)
    with pytest.raises(ValueError):
        mosaic_tiles([a, a], categorical=False)


def test_aoi_grid_snaps_outward_to_the_source_lattice():
    grid = aoi_grid((500003, 99941, 500057, 99998), "EPSG:32632", "EPSG:32632", (10.0, 10.0), (499980.0, 100020.0))
    assert grid.transform.c == 500000 and grid.transform.f == 100000 and (grid.width, grid.height) == (6, 6)


# --- Sentinel-2: one datatake over two MGRS tiles ---------------------------------------------------------

def _raster(path, data, transform, crs="EPSG:32632"):
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                       dtype=data.dtype, crs=crs, transform=transform, nodata=0) as dst:
        dst.write(data, 1)
    return path


def s2_product(folder, product_id, *, cols, x0, red, crs="EPSG:32632", acquired_at=T0, rows=6):
    folder.mkdir(parents=True)
    transform = from_origin(x0, 100000, 10, 10)
    paths = {"b04": _raster(folder / "b04.tif", np.full((rows, cols), red, "uint16"), transform),
             "b08": _raster(folder / "b08.tif", np.full((rows, cols), 5000, "uint16"), transform),
             "scl": _raster(folder / "scl.tif", np.full((rows, cols), 4, "uint8"), transform)}
    if crs != "EPSG:32632":  # Reproject each band (nearest, 2 m) into another CRS.
        for role in ("b04", "b08", "scl"):
            with rasterio.open(paths[role]) as source:
                dst_transform, width, height = calculate_default_transform(
                    source.crs, crs, source.width, source.height, *source.bounds, resolution=0.00002)
                data = np.zeros((height, width), source.dtypes[0])
                reproject(source.read(1), data, src_transform=source.transform, src_crs=source.crs,
                          dst_transform=dst_transform, dst_crs=crs, resampling=Resampling.nearest,
                          src_nodata=0, dst_nodata=0)
            paths[role] = _raster(folder / f"{role}_proj.tif", data, dst_transform, crs)
    paths["calibration_xml"] = folder / "MTD.xml"
    paths["calibration_xml"].write_text(
        f"<Product><PRODUCT_URI>{product_id}.SAFE</PRODUCT_URI><BOA_QUANTIFICATION_VALUE>10000"
        "</BOA_QUANTIFICATION_VALUE><BOA_ADD_OFFSET band_id=\"3\">-1000</BOA_ADD_OFFSET>"
        "<BOA_ADD_OFFSET band_id=\"7\">-1000</BOA_ADD_OFFSET></Product>", encoding="utf-8")
    return create_sentinel2_acquisition(
        product_id=product_id, acquired_at=acquired_at, b04_path=paths["b04"], b08_path=paths["b08"],
        scl_path=paths["scl"], calibration_xml_path=paths["calibration_xml"],
        asset_product_ids={role: product_id for role in ("b04", "b08", "scl", "calibration_xml")})


NNF = "S2B_MSIL2A_20250125T093159_N0511_R136_T32NNF_20250125T115658"
NPF = "S2B_MSIL2A_20250125T093159_N0511_R136_T32NPF_20250125T115658"


@pytest.fixture
def aoi(tmp_path):
    path = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame(geometry=[box(500000, 99940, 500060, 100000)], crs="EPSG:32632").to_file(path)
    return path


def test_two_tiles_of_one_datatake_form_an_area_weighted_mosaic(tmp_path, aoi):
    # Tile NNF: columns 0-3 (NDVI 0.6); tile NPF: columns 3-5 (NDVI 1/3); column 3 overlaps.
    first = s2_product(tmp_path / "a", NNF, cols=4, x0=500000, red=2000)
    second = s2_product(tmp_path / "b", NPF, cols=3, x0=500030, red=3000)
    result = analyze_ndvi_mosaic([second, first], aoi_path=aoi, scientific_profile=BASE)
    assert (result.grid.width, result.grid.height) == (6, 6) and result.valid.all()
    np.testing.assert_allclose(result.ndvi[:, :4], 0.6, atol=1e-6)      # Priority tile (NNF) kept on overlap.
    np.testing.assert_allclose(result.ndvi[:, 4:], 1 / 3, atol=1e-6)
    assert result.valid_coverage_percent == pytest.approx(100.0, abs=1e-4)  # Equal-area AOI vs grid.
    assert result.summary.mean == pytest.approx((24 * 0.6 + 12 / 3) / 36, abs=1e-6)
    tiles = {t.tile_id: t for t in result.tiles}
    assert tiles[NNF].selected_cells == 24 and tiles[NPF].selected_cells == 12 and tiles[NPF].overlap_cells == 6
    assert all(t.native for t in result.tiles) and any("Recouvrement de 6 cellules" in n for n in result.mosaic_notes)
    assert result.product_ids == (NNF, NPF) and set(result.calibration) == {NNF, NPF}


def test_partial_coverage_is_reported_never_filled(tmp_path, aoi):
    first = s2_product(tmp_path / "a", NNF, cols=4, x0=500000, red=2000)
    lonely = s2_product(tmp_path / "b", NPF, cols=2, x0=500200, red=3000)  # Outside the AOI.
    result = analyze_ndvi_mosaic([first, lonely], aoi_path=aoi, scientific_profile=BASE)
    assert result.valid[:, :4].all() and not result.valid[:, 4:].any() and np.isnan(result.ndvi[:, 4:]).all()
    assert result.valid_coverage_percent == pytest.approx(400 / 6, abs=1e-4)
    assert any("aucune intersection" in note for note in result.mosaic_notes) and result.product_ids == (NNF,)


def test_tile_in_another_crs_is_aligned_without_inventing_values(tmp_path, aoi):
    first = s2_product(tmp_path / "a", NNF, cols=4, x0=500000, red=2000)
    other = s2_product(tmp_path / "b", NPF, cols=3, x0=500030, red=3000, crs="EPSG:4326")
    result = analyze_ndvi_mosaic([first, other], aoi_path=aoi, scientific_profile=BASE)
    tiles = {t.tile_id: t for t in result.tiles}
    assert tiles[NNF].native and not tiles[NPF].native and tiles[NPF].crs == "EPSG:4326"
    from_other = result.valid & (np.arange(6) >= 4)[None, :]
    assert from_other.any()  # Interior cells of the reprojected tile.
    np.testing.assert_allclose(result.ndvi[from_other], 1 / 3, atol=1e-3)
    assert result.valid_coverage_percent <= 100.0


@pytest.mark.parametrize("ids, instants, message", [
    ([NNF, NPF.replace("R136", "R093")], [T0, T0], "Orbites"),
    ([NNF, NPF], [T0, T0 + timedelta(hours=1)], "incompatibles"),
    ([NNF, NNF.replace("T093159", "T093200")], [T0, T0], "double"),
])
def test_incompatible_groups_are_refused(ids, instants, message):
    with pytest.raises(ValueError, match=message):
        validate_acquisition_group(ids, instants)


def test_local_grouping_never_mixes_dates():
    from src.services.local_preview import LocalObservation, group_local_observations

    obs = [LocalObservation(T0, None, NNF), LocalObservation(T0 + timedelta(seconds=3), None, NPF),
           LocalObservation(T0 + timedelta(days=10), None, NNF.replace("20250125", "20250204"))]
    groups = group_local_observations(obs)
    assert [len(group) for group in groups] == [2, 1]


# --- WorldCover: AOI across two 3° tiles ------------------------------------------------------------------

class MultiTileHttp:
    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []

    def policy(self, url):
        assert url.startswith("https://esa-worldcover.s3.eu-central-1.amazonaws.com/")
        return url, None

    def request(self, method, url, *, headers=None, **kwargs):
        payload = next(value for name, value in self.payloads.items() if name in url)
        self.calls.append((method, url))
        if method == "HEAD":
            return ExternalResponse(200, {"Content-Length": str(len(payload))}, b"")
        start, end = (int(v) for v in headers["Range"].removeprefix("bytes=").split("-"))
        return ExternalResponse(206, {}, payload[start:end + 1])


def test_worldcover_aoi_across_two_tiles_keeps_per_tile_provenance(tmp_path):
    res = 1 / 12000
    west = write_cog(tmp_path / "w.tif", np.full((600, 600), 10, np.uint8), crs="EPSG:4326",
                     transform=from_origin(11.95, 0.45, res, res), nodata=0, block=256)
    east = write_cog(tmp_path / "e.tif", np.full((600, 600), 50, np.uint8), crs="EPSG:4326",
                     transform=from_origin(12.0, 0.45, res, res), nodata=0, block=256)
    client = MultiTileHttp({"N00E009": west.read_bytes(), "N00E012": east.read_bytes()})
    store = LayerFileStore(tmp_path / "external")
    repository = FileLayerRepository(store)
    bounds = (11.99, 0.41, 12.01, 0.43)
    windows = fetch_worldcover_windows(client, store, repository, aoi_id="frontiere", wgs84_bounds=bounds)
    assert [w[0] for w in windows] == ["N00E009", "N00E012"]
    for tile_name, path, descriptor, report in windows:
        assert descriptor.provenance["source_url"].endswith(f"{tile_name}_Map.tif") and not report["reused"]
        with rasterio.open(path) as window:
            left, _, right, _ = window.bounds
            assert (right <= 12.0 + res) if tile_name == "N00E009" else (left >= 12.0 - res)
    with pytest.raises(ValueError, match="plusieurs tuiles"):
        fetch_worldcover_window(client, store, repository, aoi_id="frontiere", wgs84_bounds=bounds)
    calls = len(client.calls)
    again = fetch_worldcover_windows(client, store, repository, aoi_id="frontiere", wgs84_bounds=bounds)
    assert len(client.calls) == calls and all(w[3]["reused"] for w in again)


def test_categorical_join_over_tiles_is_consistent(tmp_path):
    from src.services.multisource import join_categorical_tiles
    from src.geo.temporal import RasterGrid

    grid = RasterGrid(rasterio.crs.CRS.from_epsg(32632), (10, 10), from_origin(500000, 100000, 10, 10), 6, 6)
    aoi = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame(geometry=[box(500000, 99940, 500060, 100000)], crs="EPSG:32632").to_file(aoi)
    west = np.full((6, 4), 10, np.uint8)
    east = np.full((6, 3), 95, np.uint8)
    east[:, 0] = 50  # Overlapping column disagrees with the west tile.
    tiles = []
    for name, data, x0 in (("west", west, 500000), ("east", east, 500030)):
        path = write_cog(tmp_path / f"{name}.tif", data, transform=from_origin(x0, 100000, 10, 10), nodata=0, block=16)
        descriptor = WorldCoverAdapter().describe_local(path, retrieved_at=T0)
        tiles.append((path, descriptor))
    values = np.full((6, 6), 0.5, np.float32)
    weights = np.full((6, 6), 100.0)
    join, codes, layer_valid, mosaic = join_categorical_tiles(
        grid, values, np.ones((6, 6), bool), observed_at=T0, tiles=tiles, aoi_path=aoi, pixel_area_weights=weights)
    assert mosaic.overlap_cells == 6 and mosaic.conflicting_cells == 6 and layer_valid.all()
    zones = {z.code: z for z in join.zones}
    # Priority by identifier: "east" precedes "west", so the overlap column takes the east class (50).
    assert [t.tile_id for t in mosaic.contributions] == ["east", "west"]
    assert zones[10].pixel_count == 18 and zones[50].pixel_count == 6 and zones[95].pixel_count == 12
    assert sum(z.pixel_count for z in join.zones) == join.layer_coverage_pixels == 36
    assert sum(z.area_m2 for z in join.zones) == pytest.approx(3600)
    other = WorldCoverAdapter("v100").describe_local(tiles[1][0], retrieved_at=T0)
    with pytest.raises(ValueError, match="versions"):
        join_categorical_tiles(grid, values, np.ones((6, 6), bool), observed_at=T0,
                               tiles=[tiles[0], (tiles[1][0], other)], aoi_path=aoi)


# --- Sentinel-1: two MGRS windows of the same month --------------------------------------------------------

def test_radar_mosaic_nearest_overlap_footprint_and_refusals(tmp_path):
    from src.data.sentinel1 import describe_window, parse_mosaic_item
    from src.services.sentinel1_layer import load_radar_mosaic
    from tests.test_external_sources_fetch import mosaic_item

    aoi = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame(geometry=[box(500000, 99880, 500120, 100000)], crs="EPSG:32632").to_file(aoi)  # 6×6 at 20 m.
    west = np.full((6, 4), 0.1, np.float32)
    west[0, 0] = -32768
    east = np.full((6, 3), 0.01, np.float32)
    layers = []
    for name, data, x0, item in (("w", west, 500000, "Sentinel-1_IW_mosaic_2025_M01_32NNF_0_0"),
                                 ("e", east, 500060, "Sentinel-1_IW_mosaic_2025_M01_32NPF_0_0")):
        path = write_cog(tmp_path / f"{name}.tif", data, transform=from_origin(x0, 100000, 20, 20), nodata=-32768,
                         block=16)
        descriptor = describe_window(path, parse_mosaic_item(mosaic_item(id=item)), "VV", retrieved_at=T0)
        layers.append((path, descriptor))
    layer = load_radar_mosaic(list(reversed(layers)), aoi_path=aoi)
    assert layer.grid.resolution == (20.0, 20.0) and (layer.grid.width, layer.grid.height) == (6, 6)
    assert layer.invalid_counts["nodata"] == 1 and layer.invalid_counts["hors_emprise"] == 0
    np.testing.assert_allclose(layer.db[1:, :4], -10.0, atol=1e-5)    # NNF has priority on the overlap column 3.
    np.testing.assert_allclose(layer.db[:, 4:], -20.0, atol=1e-5)
    assert layer.coverage_percent == pytest.approx(35 / 36 * 100) and len(layer.tiles) == 2
    assert "recouvrement 6 cellules" in layer.mosaic_notes[0]
    vh = describe_window(layers[1][0], parse_mosaic_item(mosaic_item(id="Sentinel-1_IW_mosaic_2025_M01_32NPF_0_0")),
                         "VH", retrieved_at=T0)
    with pytest.raises(ValueError, match="polarisations"):
        load_radar_mosaic([layers[0], (layers[1][0], vh)], aoi_path=aoi)


# --- Standard / advanced modes ----------------------------------------------------------------------------

def test_reference_profiles_cover_three_territories_and_akanda_is_unchanged():
    assert set(REFERENCE_PROFILES) == {"akanda", "libreville", "owendo"}
    assert BASE.fingerprint == "c78ff691b61d9f7701981c8071954a68e2878669f0919e3187ff641248af207a"
    assert len({p.fingerprint for p in REFERENCE_PROFILES.values()}) == 3
    assert all(p.experimental for p in REFERENCE_PROFILES.values())
    assert standard_configuration("owendo").effective is reference_profile("owendo")
    with pytest.raises(ValueError):
        reference_profile("port-gentil")


def test_advanced_configuration_is_validated_versioned_and_reproducible():
    same = advanced_configuration("akanda", change_threshold=0.10, min_comparable_coverage_percent=5.0,
                                  scl_policy="usable_land")
    assert same.effective is BASE and not same.overrides
    first = advanced_configuration("akanda", change_threshold=0.15, min_comparable_coverage_percent=50)
    second = advanced_configuration("akanda", min_comparable_coverage_percent=50.0, change_threshold=0.15)
    assert first.effective == second.effective and first.effective.fingerprint == second.effective.fingerprint
    assert first.effective.fingerprint != BASE.fingerprint and BASE.change_threshold == 0.10
    provenance = first.provenance
    assert provenance["reference_fingerprint"] == BASE.fingerprint and provenance["validated"] is False
    assert provenance["overrides"] == {"change_threshold": 0.15, "min_comparable_coverage_percent": 50.0}
    assert json.dumps(provenance)  # Serializable for exports.
    assert "observation" not in ADVANCED_SCL_POLICIES and "land_surface" not in ADVANCED_SCL_POLICIES
    for kwargs in ({"change_threshold": 0.01}, {"change_threshold": 0.6}, {"min_comparable_coverage_percent": 0},
                   {"scl_policy": "observation"}, {"scl_policy": "land_surface"},
                   {"change_threshold": float("nan")}):
        with pytest.raises(ValueError):
            advanced_configuration("akanda", **kwargs)


def test_advanced_profile_changes_cache_keys(local_data):
    from src.services.local_preview import list_local_observations, local_group_key, local_preview_key

    observations = list_local_observations(local_data)
    derived = advanced_configuration("akanda", change_threshold=0.15).effective
    base_key = local_preview_key(*observations, local_data, scientific_profile=BASE)
    derived_key = local_preview_key(*observations, local_data, scientific_profile=derived)
    assert base_key != derived_key and derived.fingerprint in derived_key and BASE.fingerprint in base_key
    assert local_group_key(observations[-1:], local_data, BASE) != local_group_key(observations[-1:], local_data, derived)


# --- OSM volume estimate ----------------------------------------------------------------------------------

def test_osm_estimate_blocks_oversized_queries_before_any_geometry(tmp_path):
    from src.data.openstreetmap import fetch_osm_snapshot

    class Counting:
        def __init__(self):
            self.queries = []

        def request(self, method, url, *, data=None, **kwargs):
            self.queries.append(data["data"])
            return ExternalResponse(200, {}, json.dumps({"elements": [{"type": "count", "tags": {
                "total": "50000", "ways": "49990", "relations": "10"}}]}).encode())

    client = Counting()
    store = LayerFileStore(tmp_path / "external")
    with pytest.raises(ValueError, match="plafond"):
        fetch_osm_snapshot(client, store, FileLayerRepository(store), wgs84_bounds=(9.4, 0.4, 9.45, 0.45),
                           estimate=True, max_bytes=10_000_000)
    assert len(client.queries) == 1 and client.queries[0].endswith("out count;")
    assert not store.keys("openstreetmap")
