"""src.indicators split: historical import path, pinned profile, zone files from anywhere; offline."""

import shutil

import geopandas as gpd
import pytest
from shapely.geometry import box

import src.indicators.assembly as assembly
import src.indicators.inventory as inventory
import src.indicators.profiles as profiles
import src.indicators.quality as quality
import src.services.territorial_indicators as facade
from src.indicators.model import AVAILABLE, UNAVAILABLE
from src.indicators.zone import metric_zone, zone_area_km2, zone_sha256
from tests.test_territorial_indicators import store_layers, write_aoi

# Fingerprint of AFRICA_DEFAULT_V1 1.0.0 (23/10/2026). A change means a new profile, never an edit of this one.
AFRICA_DEFAULT_V1_FINGERPRINT = "b60d7ee3fd0f7040ab4532673e76c081e5d61eaa584559235f19774a320b1164"


def test_historical_import_path_exposes_the_same_objects():
    assert facade.build_indicators is assembly.build_indicators
    assert facade.compute_territorial_indicators is assembly.compute_territorial_indicators
    assert facade.indicator_inputs is inventory.indicator_inputs
    assert facade.assess_change is quality.assess_change
    assert facade.AFRICA_DEFAULT_V1 is profiles.AFRICA_DEFAULT_V1
    assert all(hasattr(facade, name) for name in facade.__all__)


def test_reference_profile_fingerprint_is_unchanged():
    assert profiles.AFRICA_DEFAULT_V1.fingerprint == AFRICA_DEFAULT_V1_FINGERPRINT


def test_zone_file_outside_the_aoi_folder_gives_the_same_inputs(tmp_path):
    zone = store_layers(tmp_path)
    elsewhere = tmp_path / "territories" / "test.geojson"
    elsewhere.parent.mkdir()
    shutil.copyfile(zone, elsewhere)
    expected = inventory.indicator_inputs(tmp_path, "test")
    moved = inventory.inputs_for_zone(tmp_path, "test", elsewhere)
    assert moved.key == expected.key and moved.aoi_path == elsewhere.resolve()
    before, after = (assembly.compute_territorial_indicators(i) for i in (expected, moved))
    assert [(i.indicator_id, i.value) for i in before.indicators] == [(i.indicator_id, i.value) for i in after.indicators]


def test_arbitrary_zone_without_stored_layers_is_unavailable_not_estimated(tmp_path):
    store_layers(tmp_path)
    custom = write_aoi(tmp_path / "custom.geojson", x0=503_000.0, size=2000.0)   # Another geometry, no layers.
    result = assembly.compute_territorial_indicators(inventory.inputs_for_zone(tmp_path, "custom-zone", custom))
    assert set(result.missing) == {"ESA WorldCover", "OpenStreetMap"}
    area = result.get("aoi.area_km2")
    assert area.status == AVAILABLE and area.value == pytest.approx(4.0, rel=2e-3)
    others = [i for i in result.indicators if i.indicator_id != "aoi.area_km2"]
    assert others and all(i.status == UNAVAILABLE and i.value is None for i in others)
    assert result.aoi_sha256 == zone_sha256(custom)


@pytest.mark.parametrize("lon, lat, epsg", [(9.45, 0.45, 32632), (27.48, -11.66, 32735), (-17.45, 14.70, 32628)],
                         ids=["Akanda", "Lubumbashi (sud)", "Dakar (ouest)"])
def test_zone_metric_projection_follows_the_zone_across_africa(tmp_path, lon, lat, epsg):
    path = tmp_path / "zone.geojson"
    gpd.GeoDataFrame(geometry=[box(lon, lat, lon + 0.01, lat + 0.01)], crs=4326).to_file(path)
    geometry, crs = metric_zone(path)
    assert crs.to_epsg() == epsg
    assert geometry.area / 1e6 == pytest.approx(zone_area_km2(path), rel=2e-3)
