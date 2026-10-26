"""Scientific non-regression on the real Akanda data (skipped when the local data are absent).

Reference values were computed on 22/10/2026 from the verified local
acquisitions (15 and 25 January 2025), the WorldCover v200 window and the
Sentinel-1 January 2025 IW mosaic windows. They detect unintended changes of
the processing chain; they are not an accuracy validation. No network.

Sentinel-2 values were updated on 26/10/2026 after the lossless AOI clip fix
(docs/lossless_aoi_clip.md): the 22/10 values came from JPEG 2000 clips
re-encoded with loss. The new values are identical on the full tiles and on
the demo-pack subsets, which never went through that re-encoding.
"""

import socket

import geopandas as gpd
import pytest

from src.config import DATA_DIR
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as PROFILE

AOI = DATA_DIR / "aoi" / "akanda.geojson"
pytestmark = pytest.mark.skipif(
    not AOI.exists() or not (DATA_DIR / "external" / "catalogue").is_dir(),
    reason="Données réelles Akanda absentes (non versionnées).")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def context():
    from src.services.external_layers import open_store, stored_layers
    from src.services.local_preview import list_local_observations

    observations = {o.acquired_at.date().isoformat(): o for o in list_local_observations(DATA_DIR)}
    if not {"2025-01-15", "2025-01-25"} <= set(observations):
        pytest.skip("Acquisitions de référence absentes.")
    bounds = tuple(float(v) for v in gpd.read_file(AOI).to_crs("EPSG:4326").total_bounds)
    _, repository = open_store(DATA_DIR)
    layers = [layer for layer in stored_layers(repository, "esa-worldcover", bounds)
              if layer.descriptor.provenance.get("aoi_id") == "akanda"]
    if not layers:
        pytest.skip("Fenêtre WorldCover absente.")
    return observations, bounds, repository, layers[0]


def test_single_date_ndvi_and_worldcover_classes(context):
    from src.services.local_preview import analyze_local_observation
    from src.services.multisource import join_categorical_raster

    observations, _, _, worldcover = context
    observation = analyze_local_observation(observations["2025-01-25"], DATA_DIR, scientific_profile=PROFILE)
    assert (observation.grid.width, observation.grid.height) == (1201, 1201)
    assert observation.summary.count == 930515  # 919967 avec découpage avec perte.
    assert observation.summary.median == pytest.approx(0.5042445, abs=1e-6)
    assert observation.valid_coverage_percent == pytest.approx(64.512036, abs=1e-5)
    join = join_categorical_raster(observation.grid, observation.ndvi, observation.valid,
                                   observed_at=observation.acquired_at_utc, layer_path=worldcover.path,
                                   descriptor=worldcover.descriptor, aoi_path=AOI)
    zones = {z.code: z for z in join.zones}
    assert (zones[10].pixel_count, zones[50].pixel_count, zones[95].pixel_count) == (424171, 226131, 136558)
    assert zones[10].mean == pytest.approx(0.558004, abs=1e-6)
    assert zones[50].mean == pytest.approx(0.281532, abs=1e-6)
    assert zones[95].mean == pytest.approx(0.529429, abs=1e-6)
    assert join.unmatched_pixels == 848 and round(join.temporal.offset_days) == 1120


def test_lot3_pair_join_is_unchanged(context):
    from src.services.local_preview import analyze_local_akanda
    from src.services.multisource import join_categorical_raster

    observations, _, _, worldcover = context
    pair = analyze_local_akanda(observations["2025-01-15"], observations["2025-01-25"], DATA_DIR,
                                scientific_profile=PROFILE)
    mask = pair.analysis.comparable_mask
    join = join_categorical_raster(pair.common_grid, pair.comparison.ndvi_date_2, mask,
                                   observed_at=pair.later.acquired_at_utc, layer_path=worldcover.path,
                                   descriptor=worldcover.descriptor, aoi_path=AOI)
    zones = {z.code: z for z in join.zones}
    assert int(mask.sum()) == 845383 and join.unmatched_pixels == 608  # Lot 3 : 827865 / 590 avant correctif.
    assert (zones[10].pixel_count, round(zones[10].mean, 4)) == (407423, 0.5592)
    assert (zones[50].pixel_count, round(zones[50].mean, 4)) == (206870, 0.281)
    assert (zones[95].pixel_count, round(zones[95].mean, 4)) == (98078, 0.5333)


def test_sentinel1_january_2025_windows(context):
    from src.services.external_layers import stored_layers
    from src.services.sentinel1_layer import load_radar_layer

    _, bounds, repository, _ = context
    layers = {layer.descriptor.provenance["polarization"]: layer
              for layer in stored_layers(repository, "sentinel-1-mosaic", bounds)
              if layer.descriptor.version == "Sentinel-1_IW_mosaic_2025_M01_32NNF_0_0"
              and layer.descriptor.provenance.get("aoi_id") == "akanda"}
    if set(layers) != {"VV", "VH"}:
        pytest.skip("Fenêtres Sentinel-1 de janvier 2025 absentes.")
    expected = {"VV": -7.312017, "VH": -13.841008}
    for polarization, layer in layers.items():
        radar = load_radar_layer(layer.path, layer.descriptor, aoi_path=AOI)
        assert radar.grid.resolution == (20.0, 20.0) and int(radar.valid.sum()) == 361201
        assert radar.median_db == pytest.approx(expected[polarization], abs=1e-5)


def test_pair_scientific_digest_is_unchanged(context):
    """Digest of the V0.1 pair after the lossless clip fix (b0356911… before, see docs/lossless_aoi_clip.md)."""
    from scripts.benchmark_streamlit import scientific_digest
    from src.services.local_preview import analyze_local_akanda

    observations = context[0]
    result = analyze_local_akanda(observations["2025-01-15"], observations["2025-01-25"], DATA_DIR,
                                  scientific_profile=PROFILE)
    assert scientific_digest(result) == "995a1dbfe14c3ec6b1585411ffa59e180f23a151765dad5dff4d60fb354e073b"


def test_group_path_equals_single_date_path(context):
    import numpy as np
    from src.services.local_preview import (
        analyze_local_group, analyze_local_observation, group_local_observations, list_local_observations,
    )

    observations = context[0]
    groups = group_local_observations(list_local_observations(DATA_DIR))
    group = next(g for g in groups if g[0].acquired_at.date().isoformat() == "2025-01-25")
    assert len(group) == 1  # All local products are on MGRS tile 32NNF.
    single = analyze_local_observation(observations["2025-01-25"], DATA_DIR, scientific_profile=PROFILE)
    grouped = analyze_local_group(group, DATA_DIR, scientific_profile=PROFILE)
    assert grouped.grid == single.grid and grouped.summary == single.summary
    np.testing.assert_array_equal(grouped.valid, single.valid)
