"""Scientific non-regression on the demo pack alone, through the app's reader path.

Needs only the pack (no full tiles), so it can run on any platform, e.g. Linux:
``GEONAF_DEMO_PACK_DIR=<unpacked pack> python -m pytest tests/test_demo_pack_science.py``.
Reference values are those validated on 26/10/2026, bit-identical on full tiles
and subsets after the lossless clip fix (docs/lossless_aoi_clip.md).
"""

import os
from pathlib import Path

import pytest

from src.config import DATA_DIR
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as PROFILE

PACK = Path(os.environ.get("GEONAF_DEMO_PACK_DIR") or DATA_DIR / "demo_pack" / "geonaf-demo-akanda-2025-01-v3")
AOI = PACK / "aoi" / "akanda.geojson"
pytestmark = pytest.mark.skipif(not (PACK / "PACK_MANIFEST.json").is_file(),
                                reason="Paquet de démonstration absent.")
PAIR_DIGEST = "995a1dbfe14c3ec6b1585411ffa59e180f23a151765dad5dff4d60fb354e073b"


@pytest.fixture(scope="module")
def observations():
    from src.data.demo_pack import verify_pack
    from src.services.local_preview import list_local_observations

    verify_pack(PACK)
    found = {item.acquired_at.date().isoformat(): item for item in list_local_observations(PACK)}
    assert set(found) == {"2025-01-15", "2025-01-20", "2025-01-25"}
    assert all(item.subset for item in found.values())
    return found


def test_pair_digest_matches_the_full_tile_reference(observations):
    from scripts.benchmark_streamlit import scientific_digest
    from src.services.local_preview import analyze_local_akanda

    result = analyze_local_akanda(observations["2025-01-15"], observations["2025-01-25"], PACK,
                                  scientific_profile=PROFILE)
    assert scientific_digest(result) == PAIR_DIGEST
    assert int(result.analysis.comparable_mask.sum()) == 845383


def test_single_date_and_worldcover_join(observations):
    from src.services.external_layers import open_store, stored_layers
    from src.services.local_preview import analyze_local_observation
    from src.services.multisource import join_categorical_raster
    import geopandas as gpd

    observation = analyze_local_observation(observations["2025-01-25"], PACK, scientific_profile=PROFILE)
    assert observation.summary.count == 930515
    assert observation.summary.median == pytest.approx(0.5042445, abs=1e-6)
    assert observation.valid_coverage_percent == pytest.approx(64.512036, abs=1e-5)
    bounds = tuple(float(v) for v in gpd.read_file(AOI).to_crs("EPSG:4326").total_bounds)
    _, repository = open_store(PACK)
    worldcover = [layer for layer in stored_layers(repository, "esa-worldcover", bounds)
                  if layer.descriptor.provenance.get("aoi_id") == "akanda"][0]
    join = join_categorical_raster(observation.grid, observation.ndvi, observation.valid,
                                   observed_at=observation.acquired_at_utc, layer_path=worldcover.path,
                                   descriptor=worldcover.descriptor, aoi_path=AOI)
    zones = {zone.code: zone for zone in join.zones}
    assert (zones[10].pixel_count, zones[50].pixel_count, zones[95].pixel_count) == (424171, 226131, 136558)
    assert zones[10].mean == pytest.approx(0.558004, abs=1e-6) and join.unmatched_pixels == 848


def test_series_excludes_the_low_coverage_date(observations):
    from datetime import date
    from src.services.local_preview import analyze_local_ndvi_series

    series = analyze_local_ndvi_series(list(observations.values()), PACK, scientific_profile=PROFILE,
                                       period=(date(2025, 1, 1), date(2025, 1, 31)))
    included = {item.acquired_at_utc.date().isoformat(): item.included for item in series.dates}
    assert included == {"2025-01-15": True, "2025-01-20": False, "2025-01-25": True}
    assert series.common_coverage_percent == pytest.approx(58.6125205, abs=1e-6)


def test_boundary_referentials_are_available_and_never_official():
    from src.data.territory_registry import COMMUNITY, HUMANITARIAN
    from src.services.territory_catalog import admin_territories

    expected = {"gbOpen": COMMUNITY, "gbHumanitarian": HUMANITARIAN}
    for release, status in expected.items():
        units = admin_territories(PACK, release, "ADM2")
        assert units and {unit.legal_status for unit in units} == {status}
        assert not any(unit.record.is_official for unit in units)
        assert all(unit.geometry_path.is_file() for unit in units)
