"""Sentinel-2 acquisition pairs on verified local data: selection, gates, provenance, resources, pack v3; offline."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import pickle
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from src.config import DATA_DIR
from src.data.acquisition_manifest import build_acquisition_manifest, write_acquisition_manifest
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as AKANDA
from src.indicators.assembly import build_s2_transition_indicators, build_s2_vegetation_indicator
from src.indicators.model import AVAILABLE, ESTIMATION, UNAVAILABLE
from src.indicators.profiles import EXPERIMENTAL, S2_NDVI_TRANSITION_V0, S2_NDVI_VEGETATION_V0
from src.indicators.s2_inventory import s2_vegetation_inventory
from src.indicators.s2_pairs import (
    NOT_EVALUATED, NOT_PUBLISHABLE, PUBLISHABLE, REJECTED, pair_provenance, plan_pairs, s2_pair_selection,
    s2_pairs_key, satellite,
)
from src.services.resource_guards import ResourceLimits
from tests.test_local_preview import local_data  # noqa: F401  (fixture)
from tests.test_s2_vegetation_inventory import holds_arrays

PACK = Path(os.environ.get("GEONAF_DEMO_PACK_DIR") or DATA_DIR / "demo_pack" / "geonaf-demo-akanda-2025-01-v3")
AREA_RTOL = 1e-12          # Same policy as tests/test_demo_pack_science.py (float64 sums of projected weights).
FULL, NONE = np.full((2, 2), 4, "uint8"), np.full((2, 2), 9, "uint8")
NORTH, SOUTH = np.array([[4, 4], [9, 9]], "uint8"), np.array([[9, 9], [4, 4]], "uint8")


def selection(data_dir, **kwargs):
    kwargs.setdefault("available_bytes", None)      # Host free memory must not decide a test verdict.
    return s2_pair_selection(data_dir, "akanda", **kwargs)


def write_date(root, day, scl):
    """One verified 2 × 2 single-tile acquisition on 2025-01-``day``, NDVI 0.6 where SCL is valid."""
    stamp = f"202501{day:02d}T093000"
    product_id = f"S2B_MSIL2A_{stamp}_N0511_R136_T32NNF_{stamp}"
    folder = root / "raw" / "sentinel2" / product_id
    folder.mkdir(parents=True)
    paths = {}
    for role, values in (("b04", np.full((2, 2), 2000, "uint16")), ("b08", np.full((2, 2), 5000, "uint16")),
                         ("scl", scl)):
        paths[role] = folder / f"{role}.tif"
        with rasterio.open(paths[role], "w", driver="GTiff", width=2, height=2, count=1, dtype=values.dtype,
                           crs="EPSG:32632", transform=from_origin(500000, 100000, 10, 10), nodata=0) as target:
            target.write(values, 1)
    paths["calibration_xml"] = folder / "metadata.xml"
    paths["calibration_xml"].write_text(
        f"<Product><PRODUCT_URI>{product_id}.SAFE</PRODUCT_URI><BOA_QUANTIFICATION_VALUE>10000"
        "</BOA_QUANTIFICATION_VALUE><BOA_ADD_OFFSET band_id=\"3\">-1000</BOA_ADD_OFFSET>"
        "<BOA_ADD_OFFSET band_id=\"7\">-1000</BOA_ADD_OFFSET></Product>", encoding="utf-8")
    manifest = build_acquisition_manifest(
        product={"id": product_id, "collection": "sentinel-2-l2a",
                 "properties": {"datetime": f"2025-01-{day:02d}T09:30:00Z"},
                 "assets": {role: {"href": f"s3://test/{role}"} for role in paths}},
        asset_paths=paths, asset_ids={role: role for role in paths}, stac_source="https://example.org/stac",
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc), product_directory=folder,
        integrity={"mode": "strict", "evidence": {}, "source_assets": {
            role: {"status": "verified", "method": "stac_file_checksum", "algorithm": "sha256",
                   "declared_checksum_status": "valid"} for role in paths}})
    write_acquisition_manifest(manifest, folder / "acquisition_manifest.json")
    return folder


def dates(tmp_path, scl_by_day):
    for day, scl in scl_by_day.items():
        write_date(tmp_path, day, scl)
    (tmp_path / "aoi").mkdir()
    gpd.GeoDataFrame(geometry=[box(500000, 99980, 500020, 100000)], crs="EPSG:32632").to_file(
        tmp_path / "aoi" / "akanda.geojson")
    return tmp_path


@pytest.fixture
def chain_calls(monkeypatch):
    """Counts the pair computations through the historical chain."""
    import src.services.local_preview as local_preview

    calls, original = [], local_preview.analyze_local_akanda

    def counted(earlier, later, *args, **kwargs):
        calls.append((earlier.acquired_at.day, later.acquired_at.day))
        return original(earlier, later, *args, **kwargs)

    monkeypatch.setattr(local_preview, "analyze_local_akanda", counted)
    return calls


def by_days(result):
    return {(p.earlier.acquired_at_utc.day, p.later.acquired_at_utc.day): p for p in result.pairs}


# --- Planning (pure) --------------------------------------------------------------------------------------------


def fake_estimate(day, coverage, tiles=1):
    return SimpleNamespace(acquired_at_utc=datetime(2025, 1, day, 9, 30, tzinfo=timezone.utc),
                           product_ids=tuple(f"S2B_{day}_{i}" for i in range(tiles)),
                           measure=SimpleNamespace(observed_coverage_percent=coverage))


def test_plan_orders_by_recency_and_rejects_by_upper_bound_and_mosaic():
    inventory = SimpleNamespace(estimates=(fake_estimate(25, 64.5), fake_estimate(5, 90.0),
                                           fake_estimate(15, 66.7), fake_estimate(20, 2.7),
                                           fake_estimate(10, 80.0, tiles=2)))
    candidates, rejected = plan_pairs(inventory)
    labels = [(p.earlier.acquired_at_utc.day, p.later.acquired_at_utc.day) for p in candidates]
    assert labels == [(15, 25), (5, 25), (5, 15)]                    # Later date first, then the later earlier one.
    assert all(p.status == NOT_EVALUATED and p.measure is None for p in candidates)
    assert candidates[0].coverage_upper_bound_percent == 64.5         # min(66.7, 64.5).
    assert len(candidates) + len(rejected) == 10                     # Every chronological pair is kept.
    for pair in rejected:
        assert pair.status == REJECTED and pair.measure is None and pair.reasons
        days = {pair.earlier.acquired_at_utc.day, pair.later.acquired_at_utc.day}
        assert 20 in days or 10 in days
    with_20 = [p for p in rejected if 20 in {p.earlier.acquired_at_utc.day, p.later.acquired_at_utc.day}]
    assert all("Borne supérieure de couverture commune 2,7 % < 30 %" in " ".join(p.reasons) for p in with_20)
    mosaic = next(p for p in rejected if {p.earlier.acquired_at_utc.day, p.later.acquired_at_utc.day} == {10, 25})
    assert any("Mosaïque" in r for r in mosaic.reasons)


def test_upper_bound_at_the_threshold_is_a_candidate():
    inventory = SimpleNamespace(estimates=(fake_estimate(15, 30.0), fake_estimate(25, 99.0)))
    candidates, rejected = plan_pairs(inventory)
    assert len(candidates) == 1 and not rejected


# --- Selection on verified local acquisitions -------------------------------------------------------------------


def test_default_pair_on_the_common_mask_of_the_historical_chain(local_data, chain_calls):
    from src.geo.change_analysis import load_equal_area_aoi
    from src.services.local_preview import analyze_local_akanda, list_local_observations

    result = selection(local_data)
    pair = result.default_pair()
    assert pair is not None and pair.label == "15/01/2025 → 25/01/2025" and chain_calls == [(15, 25)]
    assert pair.status == PUBLISHABLE and pair.assessment.reliability == "faible"
    assert pair.assessment.scientific_status == EXPERIMENTAL
    earlier, later = list_local_observations(local_data)
    chain = analyze_local_akanda(earlier, later, local_data, scientific_profile=AKANDA)
    assert pair.measure.common_m2 == chain.analysis.comparable_area_m2                    # Bit for bit.
    assert pair.measure.common_coverage_percent <= pair.coverage_upper_bound_percent
    assert pair.measure.common_m2 == pytest.approx(0.5 * pair.measure.zone_m2, rel=1e-3)   # SCL 4 and 5 only.
    assert result.change_threshold == AKANDA.change_threshold == 0.10                      # Variation threshold,
    assert result.profile.threshold == 0.4                                                 # not the class one.
    assert not holds_arrays(result)
    assert load_equal_area_aoi(local_data / "aoi" / "akanda.geojson").area_m2 == pair.measure.zone_m2


def test_pairs_below_the_bound_are_never_computed(tmp_path, chain_calls):
    result = selection(dates(tmp_path, {10: FULL, 20: np.array([[4, 9], [9, 9]], "uint8"), 30: FULL}))
    pairs = by_days(result)
    assert chain_calls == [(10, 30)] and result.default_pair() is pairs[10, 30]
    for key in ((10, 20), (20, 30)):
        assert pairs[key].status == REJECTED and pairs[key].coverage_upper_bound_percent == pytest.approx(25.0,
                                                                                                         rel=1e-3)
        assert pairs[key].measure is None
        indicators = build_s2_transition_indicators(pairs[key], result)
        assert all(i.status == UNAVAILABLE and i.value is None and i.shares == () for i in indicators)


def test_upper_bound_is_never_a_proof_of_comparability(tmp_path):
    result = selection(dates(tmp_path, {10: NORTH, 20: SOUTH}))            # 50 % each, nothing in common.
    (pair,) = result.pairs
    assert pair.coverage_upper_bound_percent == pytest.approx(50.0, rel=1e-3)
    assert pair.status == NOT_PUBLISHABLE and pair.measure.common_m2 == 0
    assert "Aucune surface observée commune" in pair.reasons[0] and result.default_pair() is None


def test_evaluation_stops_at_the_most_recent_publishable_pair(tmp_path, chain_calls):
    result = selection(dates(tmp_path, {10: FULL, 20: FULL, 30: FULL}))
    assert chain_calls == [(20, 30)] and result.default_pair().label == "20/01/2025 → 30/01/2025"
    pairs = by_days(result)
    for key in ((10, 30), (10, 20)):
        assert pairs[key].status == NOT_EVALUATED and "Paire plus récente retenue" in pairs[key].reasons[0]


def test_evaluation_limit_bounds_the_computations(tmp_path, chain_calls):
    data_dir = dates(tmp_path, {10: NORTH, 20: SOUTH, 30: NORTH})
    limited = selection(data_dir, max_pairs=1)
    assert chain_calls == [(20, 30)] and limited.default_pair() is None and limited.max_pairs == 1
    assert "Limite de 1 paire(s)" in by_days(limited)[10, 30].reasons[0]
    chain_calls.clear()
    wider = selection(data_dir, max_pairs=3)
    assert chain_calls == [(20, 30), (10, 30)] and wider.default_pair().label == "10/01/2025 → 30/01/2025"
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="paires"):
            selection(data_dir, max_pairs=value)


# --- Refusals and errors ----------------------------------------------------------------------------------------


def test_permissive_scl_policy_and_foreign_inputs_are_refused(local_data, chain_calls, tmp_path_factory):
    with pytest.raises(ValueError, match="refusée"):
        selection(local_data, scientific_profile=replace(AKANDA, scl_policy="observation"))
    with pytest.raises(ValueError, match="autre zone"):
        selection(local_data, scientific_profile=replace(AKANDA, aoi_id="owendo"))
    other = s2_vegetation_inventory(local_data, "akanda", available_bytes=None,
                                    profile=replace(S2_NDVI_VEGETATION_V0, version="0.2.0"))
    with pytest.raises(ValueError, match="autres profils"):
        selection(local_data, inventory=other)
    with pytest.raises(ValueError, match="autre géométrie"):
        selection(local_data, inventory=replace(other, aoi_sha256="0" * 64))
    assert chain_calls == []


def test_resource_guard_refuses_before_any_pair_computation(local_data, chain_calls):
    inventory = s2_vegetation_inventory(local_data, "akanda", available_bytes=None)
    result = selection(local_data, inventory=inventory, limits=ResourceLimits(warn_cells=1, max_cells=2))
    assert chain_calls == [] and result.default_pair() is None
    (pair,) = result.pairs
    assert pair.status == NOT_EVALUATED and "refusé avant traitement" in pair.reasons[0]
    assert any("refusé avant traitement" in note for note in result.notes)


def test_misaligned_grids_give_no_transition(local_data, monkeypatch):
    import src.indicators.s2_pairs as s2_pairs

    monkeypatch.setattr(s2_pairs, "same_lattice_pair", lambda *args: False)
    (pair,) = selection(local_data).pairs
    assert pair.status == NOT_PUBLISHABLE and pair.measure is None
    assert "non alignées" in pair.reasons[0] and "aucun rééchantillonnage" in pair.reasons[0]
    assert all(i.value is None for i in build_s2_transition_indicators(pair, selection(local_data)))


def test_altered_acquisition_after_inventory_gives_a_public_error(local_data):
    inventory = s2_vegetation_inventory(local_data, "akanda", available_bytes=None)
    later = sorted((local_data / "raw" / "sentinel2").glob("*"))[-1]
    (later / "b04.tif").write_bytes(b"changed")
    (pair,) = selection(local_data, inventory=inventory).pairs
    assert pair.status == NOT_PUBLISHABLE and "impossible" in pair.reasons[0]
    assert str(local_data) not in pair.reasons[0]


def test_inconsistent_inventory_bound_is_refused(local_data):
    inventory = s2_vegetation_inventory(local_data, "akanda", available_bytes=None)
    shrunk = tuple(replace(e, measure=replace(e.measure, observed_m2=0.4 * e.measure.observed_m2))
                   for e in inventory.estimates)                          # Bound 20 % < real 50 %…
    strict = replace(S2_NDVI_TRANSITION_V0, min_common_coverage_percent=10.0)
    (pair,) = selection(local_data, inventory=replace(inventory, estimates=shrunk), profile=strict).pairs
    assert pair.status == NOT_PUBLISHABLE and "incohérentes" in pair.reasons[0]


def test_no_pair_without_two_acquisitions(tmp_path):
    result = selection(dates(tmp_path, {10: FULL}))
    assert result.pairs == () and result.default_pair() is None
    assert any("Aucune paire candidate" in note for note in result.notes)


# --- Cache key ----------------------------------------------------------------------------------------------------


def test_cache_key_covers_zone_acquisitions_fingerprints_and_profiles(local_data):
    key = s2_pairs_key(local_data, "akanda")
    assert key == s2_pairs_key(local_data, "akanda")
    assert S2_NDVI_TRANSITION_V0.fingerprint in key and AKANDA.fingerprint in key[1]
    assert S2_NDVI_VEGETATION_V0.fingerprint in key[1]
    assert key != s2_pairs_key(local_data, "akanda", max_pairs=1)
    assert key != s2_pairs_key(local_data, "akanda",
                               profile=replace(S2_NDVI_TRANSITION_V0, min_common_coverage_percent=40.0))
    assert key != s2_pairs_key(local_data, "akanda", scientific_profile=replace(AKANDA, version="9"))
    manifest = sorted((local_data / "raw" / "sentinel2").glob("*"))[-1] / "acquisition_manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert key != s2_pairs_key(local_data, "akanda")
    aoi = local_data / "aoi" / "akanda.geojson"
    before = s2_pairs_key(local_data, "akanda")
    aoi.write_bytes(aoi.read_bytes() + b" ")
    assert before != s2_pairs_key(local_data, "akanda")


def test_cached_selection_holds_only_numbers(local_data):
    result = selection(local_data)
    assert not holds_arrays(result) and len(pickle.dumps(result)) < 50_000


# --- Provenance and assembly -------------------------------------------------------------------------------------


def test_provenance_and_indicators_are_complete_and_without_local_paths(local_data):
    result = selection(local_data)
    pair = result.default_pair()
    provenance = pair_provenance(pair, result)
    text = json.dumps(provenance, ensure_ascii=False)
    assert str(local_data) not in text and str(local_data).replace("\\", "/") not in text and "\\\\" not in text
    assert provenance["earlier"]["acquired_at_utc"].startswith("2025-01-15T09:30")
    assert provenance["later"]["product_ids"] == ["S2B_TEST_25"]
    assert provenance["earlier"]["tiles"][0]["manifest_sha256"] and provenance["later"]["tiles"][0]["scl_sha256"]
    assert provenance["profiles"]["scientific"]["fingerprint"] == AKANDA.fingerprint
    assert provenance["profiles"]["vegetation"]["fingerprint"] == S2_NDVI_VEGETATION_V0.fingerprint
    assert provenance["profiles"]["transition"]["fingerprint"] == S2_NDVI_TRANSITION_V0.fingerprint
    assert provenance["ndvi_class_threshold"] == 0.4
    assert provenance["profiles"]["scientific"]["ndvi_variation_threshold"] == 0.10
    assert provenance["common_coverage_percent"] == pair.measure.common_coverage_percent
    assert provenance["scientific_status"] == EXPERIMENTAL and provenance["reliability"] == "faible"
    assert any("Intervalle court" in w for w in provenance["warnings"])
    indicators = build_s2_transition_indicators(pair, result)
    assert str(local_data) not in repr(indicators)
    assert [i.indicator_id for i in indicators] == ["s2.pair.common_km2", "s2.pair.delta_ndvi_mean",
                                                   "s2.pair.class_transitions_km2"]
    for indicator in indicators:
        assert indicator.status == AVAILABLE and indicator.result_kind == ESTIMATION
        assert indicator.scientific_status == EXPERIMENTAL and indicator.reliability == "faible"
        assert indicator.dates.observation_start_utc.day == 15 and indicator.dates.observation_end_utc.day == 25
        assert indicator.dates.ingested_at_utc == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert "S2B_TEST_15 → S2B_TEST_25" in indicator.source.version
        assert indicator.profile_fingerprint == S2_NDVI_TRANSITION_V0.fingerprint
        assert indicator.coverage_percent == pair.measure.common_coverage_percent
        assert any("Non comparable aux indicateurs WorldCover" in limit for limit in indicator.limits)
        assert any("distinct du seuil de variation" in limit for limit in indicator.limits)
        assert any("Intervalle court" in reason for reason in indicator.reliability_reasons)
        for fragment in ("usable_land", AKANDA.profile_id, S2_NDVI_TRANSITION_V0.profile_id, "seuil de classe NDVI 0,4"):
            assert fragment in indicator.method
        texts = " ".join(indicator.limits + indicator.reliability_reasons + (indicator.label,)).lower()
        assert not any(word in texts.replace("ni déforestation, ni reboisement", "")
                       for word in ("déforestation", "reboisement", "perte de végétation"))
    transitions = indicators[2]
    keys = [item.key for item in transitions.breakdown]
    assert keys == ["down_m2", "up_m2", "stayed_above_m2", "stayed_below_m2", "net_m2"]
    assert sum(item.value for item in transitions.breakdown[:4]) == pytest.approx(indicators[0].value, rel=1e-12)
    assert [label for label, _ in transitions.breakdown[0].detail] == ["Seuil 0,3", "Seuil 0,4", "Seuil 0,5"]


def test_l4_indicators_stay_apart_from_worldcover_osm_and_l3(local_data):
    from src.indicators.assembly import build_indicators

    result = selection(local_data)
    l4 = {i.indicator_id for i in build_s2_transition_indicators(result.default_pair(), result)}
    inventory = s2_vegetation_inventory(local_data, "akanda", available_bytes=None)
    l3 = {build_s2_vegetation_indicator(inventory.estimates[-1], inventory).indicator_id}
    others = {i.indicator_id for i in build_indicators(aoi_id="akanda", aoi_path=local_data / "aoi" / "akanda.geojson",
                                                       worldcover=None, osm=None).indicators}
    assert l4 and all(i.startswith("s2.pair.") for i in l4)
    assert not l4 & l3 and not l4 & others


def test_satellite_from_the_product_identifier():
    assert satellite("S2B_MSIL2A_20250115T093239_N0511_R136_T32NNF_20250115T120528") == "Sentinel-2B"
    assert satellite("S2A_MSIL2A_20250120T093321_N0511_R136_T32NNF_20250120T125148") == "Sentinel-2A"
    assert satellite("S2B_TEST_25") == "Sentinel-2B" and satellite("LC09_X") is None


# --- Pack v3 reference values (reproducibility, not scientific validation) ---------------------------------------

# Computed on 29/10/2026 (Windows, Python 3.10) by s2_pair_selection; areas in m². Surfaces and NDVI means are
# float64 sums of projected cell weights: AREA_RTOL (1e-12) as for the other pack v3 references. Medians are
# pixel values (float32) picked by cumulative weight, the pixel count is exact.
PACK_PAIR = dict(common_m2=84465662.8600188, coverage=58.61252054667379, zone_m2=144108566.0064011,
                 valid_1_m2=96157195.72065385, valid_2_m2=92967369.73900154,
                 mean_1=0.4976121949181785, mean_2=0.4647500514698899, delta_mean=-0.03286214344828858,
                 median_1=0.539404571056366, median_2=0.5052738785743713, delta_median=-0.026417076587677002,
                 pixels=845383, bound=64.51203583197966)
PACK_TRANSITIONS = {   # threshold: (down, up, stayed above, stayed below), m².
    0.3: (5225088.110782078, 2173788.612981338, 63795044.155127, 13271741.98112838),
    0.4: (6733098.650604916, 2791140.950792107, 53194324.83338292, 21747098.42523885),
    0.5: (8289041.817217598, 3364078.4565542545, 39664423.73566738, 33148118.850579564),
}


@pytest.fixture(scope="module")
def pack_selection():
    if not (PACK / "PACK_MANIFEST.json").is_file():
        pytest.skip("Paquet de démonstration absent.")
    from src.data.demo_pack import verify_pack

    verify_pack(PACK)
    inventory = s2_vegetation_inventory(PACK, "akanda", available_bytes=None)
    return inventory, s2_pair_selection(PACK, "akanda", inventory=inventory, available_bytes=None)


def test_pack_v3_selects_15_to_25_january(pack_selection):
    _, result = pack_selection
    pair = result.default_pair()
    assert pair.label == "15/01/2025 → 25/01/2025" and pair.status == PUBLISHABLE
    assert pair.assessment.reliability == "faible" and pair.assessment.scientific_status == EXPERIMENTAL
    assert pair.interval_days == pytest.approx(10.0, abs=1e-3) and result.notes == ()
    assert any("Intervalle court" in w for w in pair.assessment.warnings)
    statuses = {p.label: p.status for p in result.pairs}
    assert statuses == {"15/01/2025 → 25/01/2025": PUBLISHABLE, "20/01/2025 → 25/01/2025": REJECTED,
                        "15/01/2025 → 20/01/2025": REJECTED}


def test_pack_v3_reference_values(pack_selection):
    inventory, result = pack_selection
    m, expected = result.default_pair().measure, PACK_PAIR
    for name in ("common_m2", "zone_m2", "valid_1_m2", "valid_2_m2"):
        assert getattr(m, name) == pytest.approx(expected[name], rel=AREA_RTOL, abs=0), name
    assert m.common_coverage_percent == pytest.approx(expected["coverage"], rel=AREA_RTOL, abs=0)
    for name in ("mean_1", "mean_2", "delta_mean", "median_1", "median_2", "delta_median"):
        assert getattr(m, name) == pytest.approx(expected[name], rel=AREA_RTOL, abs=0), name
    assert m.common_pixels == expected["pixels"]                      # Same as the pack's class pixel counts.
    assert [s.threshold for s in m.sensitivity] == [0.3, 0.4, 0.5] and m.sensitivity[1] is m.transitions
    for s in m.sensitivity:
        values = (s.down_m2, s.up_m2, s.stayed_above_m2, s.stayed_below_m2)
        for value, reference in zip(values, PACK_TRANSITIONS[s.threshold]):
            assert value == pytest.approx(reference, rel=AREA_RTOL, abs=0), s.threshold
        assert sum(values) == pytest.approx(m.common_m2, rel=AREA_RTOL)
    # Rounded figures of the request, as a readable check of the frozen values.
    t = m.transitions
    assert round(m.common_m2 / 1e6, 2) == 84.47 and round(m.common_coverage_percent, 2) == 58.61
    assert round(m.delta_mean, 4) == -0.0329
    assert [round(v / 1e6, 2) for v in (t.down_m2, t.up_m2, t.stayed_above_m2, t.stayed_below_m2)] == \
        [6.73, 2.79, 53.19, 21.75]
    assert (m.blocks.eligible_blocks, m.blocks.covered_share) == (140, pytest.approx(109 / 140))
    # The per-date valid surfaces of the chain are the L3 observed surfaces: the bound is a real upper bound.
    found = {e.acquired_at_utc.day: e for e in inventory.estimates}
    assert m.valid_1_m2 == found[15].measure.observed_m2 and m.valid_2_m2 == found[25].measure.observed_m2
    assert result.default_pair().coverage_upper_bound_percent == pytest.approx(expected["bound"], rel=AREA_RTOL)


def test_pack_v3_pairs_with_20_january_stay_unpublished(pack_selection):
    from src.geo.change_analysis import load_equal_area_aoi
    from src.indicators.s2_pairs import _evaluate, _observation_groups

    _, result = pack_selection
    with_20 = [p for p in result.pairs if 20 in (p.earlier.acquired_at_utc.day, p.later.acquired_at_utc.day)]
    assert len(with_20) == 2
    for pair in with_20:
        assert pair.status == REJECTED and pair.measure is None
        assert all(i.value is None for i in build_s2_transition_indicators(pair, result))
    # Even computed through the chain, the 15 → 20 pair is refused on its real common coverage.
    earliest = next(p for p in with_20 if p.earlier.acquired_at_utc.day == 15)
    computed = _evaluate(earliest, _observation_groups(PACK), PACK, AKANDA, S2_NDVI_TRANSITION_V0,
                         load_equal_area_aoi(PACK / "aoi" / "akanda.geojson"))
    assert computed.status == NOT_PUBLISHABLE and "< 30 %" in computed.reasons[0]
    assert computed.measure.common_coverage_percent <= earliest.coverage_upper_bound_percent


def test_pack_v3_is_reproducible_and_leaves_the_historical_chain_untouched(pack_selection):
    from tests.test_indicators_modules import AFRICA_DEFAULT_V1_FINGERPRINT
    from tests.test_transitions_s2 import TRANSITION_FINGERPRINT, VEGETATION_FINGERPRINT
    from tests.test_vegetation_s2 import SCIENTIFIC_FINGERPRINTS
    from src.indicators.profiles import AFRICA_DEFAULT_V1

    inventory, result = pack_selection
    again = s2_pair_selection(PACK, "akanda", inventory=inventory, available_bytes=None)
    assert again.default_pair().measure == result.default_pair().measure
    assert (S2_NDVI_TRANSITION_V0.fingerprint, S2_NDVI_VEGETATION_V0.fingerprint) == (TRANSITION_FINGERPRINT,
                                                                                       VEGETATION_FINGERPRINT)
    assert AKANDA.fingerprint == SCIENTIFIC_FINGERPRINTS["akanda"]
    assert AFRICA_DEFAULT_V1.fingerprint == AFRICA_DEFAULT_V1_FINGERPRINT
    provenance = pair_provenance(result.default_pair(), result)
    assert provenance["earlier"]["satellites"] == ["Sentinel-2B"] and provenance["later"]["relative_orbits"] == ["136"]
    assert str(PACK) not in json.dumps(provenance, ensure_ascii=False)
    assert not holds_arrays(result)
