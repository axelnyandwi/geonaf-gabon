"""Key figures on the real Akanda study zone (skipped when the local layers are absent).

Reference values computed on 23/10/2026 from the stored WorldCover v200 window
and the OSM snapshot of 22/10/2026 16:57 UTC. They detect unintended changes
of the calculation chain; they are not an accuracy validation. No network.
"""

import socket

import pytest

from src.config import DATA_DIR

pytestmark = pytest.mark.skipif(
    not (DATA_DIR / "aoi" / "akanda.geojson").exists() or not (DATA_DIR / "external" / "catalogue").is_dir(),
    reason="Données réelles Akanda absentes (non versionnées).")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def result():
    from src.services.territorial_indicators import compute_territorial_indicators, indicator_inputs

    inputs = indicator_inputs(DATA_DIR, "akanda")
    if not inputs.worldcover or inputs.osm is None or inputs.osm.descriptor.version != "20261022T165705Z":
        pytest.skip("Couches de référence Akanda absentes.")
    return compute_territorial_indicators(inputs, aoi_label="Zone d'étude Akanda")


@pytest.mark.parametrize("indicator_id, expected", [
    ("aoi.area_km2", 144.109), ("landcover.land_km2", 107.439), ("landcover.vegetation_km2", 82.575),
    ("landcover.builtup_km2", 23.921), ("osm.roads_km", 635.285), ("osm.buildings_count", 36006),
    ("osm.buildings_footprint_km2", 4.4795),
])
def test_akanda_reference_values(result, indicator_id, expected):
    assert result.get(indicator_id).value == pytest.approx(expected, rel=1e-4)


def test_akanda_breakdowns_and_status(result):
    vegetation = {item.label: item.value for item in result.get("landcover.vegetation_km2").breakdown}
    assert vegetation == pytest.approx({"Végétation naturelle": 63.947, "Cultures": 0.01154, "Mangroves": 18.617},
                                       rel=1e-3)
    roads = {item.label: item.value for item in result.get("osm.roads_km").breakdown}
    assert roads["Voies de desserte"] == pytest.approx(381.207, rel=1e-4)
    assert roads["Axes principaux"] == pytest.approx(25.471, rel=1e-4)
    assert not result.missing and not result.warnings
    assert result.get("landcover.vegetation_km2").coverage_percent == pytest.approx(99.92, abs=0.01)
