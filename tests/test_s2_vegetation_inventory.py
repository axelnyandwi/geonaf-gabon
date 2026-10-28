"""Sentinel-2 vegetation estimates on real local acquisitions: SCL coherence, failures, mosaics, pack v3; offline."""

from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from src.config import DATA_DIR
from src.data.acquisition_manifest import build_acquisition_manifest, write_acquisition_manifest
from src.data.cloud_mask import SCL_MASK_POLICIES
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as AKANDA
from src.indicators.assembly import build_s2_vegetation_indicator
from src.indicators.model import AVAILABLE, ESTIMATION, UNAVAILABLE
from src.indicators.profiles import EXPERIMENTAL, S2_NDVI_VEGETATION_V0
from src.indicators.provenance import parse_utc
from src.indicators.quality import FORBIDDEN_VEGETATION_SCL, check_scl_consistency, check_vegetation_scl_policy
from src.indicators.s2_inventory import BREAKDOWN_AVAILABLE, BREAKDOWN_UNAVAILABLE, s2_vegetation_inventory
from src.services.local_preview import (
    analyze_local_group_with_scl, analyze_local_observation, group_local_observations, list_local_observations,
)
from src.services.resource_guards import ResourceLimits
from tests.test_local_preview import local_data  # noqa: F401  (fixture)

PACK = Path(os.environ.get("GEONAF_DEMO_PACK_DIR") or DATA_DIR / "demo_pack" / "geonaf-demo-akanda-2025-01-v3")
AREA_RTOL = 1e-12          # Same tolerance as tests/test_demo_pack_science.py for float surfaces.


def inventory(data_dir, **kwargs):
    kwargs.setdefault("available_bytes", None)      # Host free memory must not decide a test verdict.
    return s2_vegetation_inventory(data_dir, "akanda", **kwargs)


def holds_arrays(value) -> bool:
    if isinstance(value, np.ndarray):
        return True
    if is_dataclass(value) and not isinstance(value, type):
        return any(holds_arrays(getattr(value, f.name)) for f in fields(value))
    if isinstance(value, dict):
        return any(holds_arrays(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return any(holds_arrays(v) for v in value)
    return False


# --- NDVI / SCL coherence --------------------------------------------------------------------------------


def test_scl_classes_come_from_the_analysed_acquisition(local_data):
    group = group_local_observations(list_local_observations(local_data))[-1]
    observation, scl, provenance = analyze_local_group_with_scl(group, local_data, scientific_profile=AKANDA)
    reference = analyze_local_observation(group[0], local_data, scientific_profile=AKANDA)
    assert scl.product_id == observation.product_id == provenance[0]["product_id"] == "S2B_TEST_25"
    np.testing.assert_array_equal(scl.classes, [[2, 4], [5, 7]])          # The fixture's SCL, same grid.
    check_scl_consistency(observation, scl)
    # Asking for the SCL changes nothing in the observation itself.
    np.testing.assert_array_equal(observation.valid, reference.valid)
    np.testing.assert_array_equal(observation.ndvi, reference.ndvi)
    assert observation.valid_area_m2 == reference.valid_area_m2


@pytest.mark.parametrize("change", ["cloud on a valid pixel", "other product", "other grid"])
def test_inconsistent_scl_is_refused(local_data, change):
    group = group_local_observations(list_local_observations(local_data))[-1]
    observation, scl, _ = analyze_local_group_with_scl(group, local_data, scientific_profile=AKANDA)
    if change == "cloud on a valid pixel":
        classes = scl.classes.copy()
        classes[0, 1] = 9                                                  # Pixel (0, 1) is valid vegetation.
        scl = replace(scl, classes=classes)
    elif change == "other product":
        scl = replace(scl, product_id="S2B_TEST_15")
    else:
        scl = replace(scl, classes=scl.classes[:1], valid=scl.valid[:1])
    with pytest.raises(ValueError, match="SCL"):
        check_scl_consistency(observation, scl)


# --- SCL policy --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("policy", sorted(SCL_MASK_POLICIES))
def test_policies_accepting_clouds_shadows_or_defects_are_refused(policy):
    if SCL_MASK_POLICIES[policy] & FORBIDDEN_VEGETATION_SCL:
        with pytest.raises(ValueError, match="refusée"):
            check_vegetation_scl_policy(policy)
    else:
        check_vegetation_scl_policy(policy)


def test_forbidden_policy_stops_before_any_computation(local_data, monkeypatch):
    import src.services.local_preview as local_preview

    monkeypatch.setattr(local_preview, "analyze_local_group_with_scl",
                        lambda *a, **k: pytest.fail("Aucun calcul ne doit être lancé."))
    with pytest.raises(ValueError, match="refusée"):
        inventory(local_data, scientific_profile=replace(AKANDA, scl_policy="observation"))
    with pytest.raises(ValueError, match="autre zone"):
        inventory(local_data, scientific_profile=replace(AKANDA, aoi_id="owendo"))


# --- Inventory on local acquisitions -----------------------------------------------------------------------


def test_inventory_measures_each_acquisition_and_keeps_no_raster(local_data):
    result = inventory(local_data)
    assert [e.acquired_at_utc.day for e in result.estimates] == [15, 25] and not result.failures
    assert result.scl_policy == "usable_land" and result.scientific_profile_fingerprint == AKANDA.fingerprint
    for estimate in result.estimates:
        m = estimate.measure
        assert estimate.breakdown_status == BREAKDOWN_AVAILABLE and estimate.assessment.available
        assert m.observed_coverage_percent == pytest.approx(50.0, rel=1e-3)   # SCL 4 and 5 of [[2, 4], [5, 7]].
        assert m.vegetated_share_percent == pytest.approx(100.0)              # NDVI 0.6 everywhere.
        assert m.excluded_observed_m2 == pytest.approx(m.observed_m2, rel=1e-3)   # Classes 2 and 7.
        assert m.not_observed_m2 == 0 and m.outside_footprint_m2 == 0
        assert parse_utc(estimate.ingested_at_utc) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert result.default_estimate() is result.estimates[-1]
    assert not holds_arrays(result)


def test_indicator_carries_the_full_provenance(local_data):
    result = inventory(local_data)
    indicator = build_s2_vegetation_indicator(result.estimates[-1], result)
    assert indicator.status == AVAILABLE and indicator.result_kind == ESTIMATION
    assert indicator.scientific_status == EXPERIMENTAL and indicator.reliability == "faible"
    assert indicator.dates.observation_start_utc == indicator.dates.observation_end_utc
    assert indicator.dates.observation_start_utc.day == 25
    assert indicator.dates.ingested_at_utc == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert indicator.source.version == "S2B_TEST_25" and "Copernicus Sentinel data 2025" in indicator.source.attribution
    for fragment in ("usable_land", AKANDA.profile_id, AKANDA.version, S2_NDVI_VEGETATION_V0.profile_id,
                     S2_NDVI_VEGETATION_V0.version, "≥ 0.4", "aucune extrapolation"):
        assert fragment in indicator.method
    assert indicator.coverage_percent == pytest.approx(50.0, rel=1e-3)
    assert indicator.shares == (("surface observée exploitable", pytest.approx(100.0)),)
    assert any("pas une mesure directe" in limit for limit in indicator.limits)
    assert indicator.profile_fingerprint == S2_NDVI_VEGETATION_V0.fingerprint
    keys = [item.key for item in indicator.breakdown]
    assert keys == ["observed", "vegetated", "not_vegetated", "excluded_observed", "not_observed", "other_invalid",
                    "outside_footprint"]
    assert [label for label, _ in indicator.breakdown[1].detail] == ["NDVI ≥ 0.3", "NDVI ≥ 0.4", "NDVI ≥ 0.5"]


def test_missing_acquisitions_give_no_estimate(local_data, tmp_path_factory):
    empty_dir = tmp_path_factory.mktemp("empty")
    (empty_dir / "aoi").mkdir()
    (empty_dir / "aoi" / "akanda.geojson").write_bytes((local_data / "aoi" / "akanda.geojson").read_bytes())
    empty = inventory(empty_dir)
    assert empty.estimates == () and empty.default_estimate() is None
    assert any("Aucune acquisition Sentinel-2 locale vérifiée pour 2025" in note for note in empty.notes)
    assert inventory(local_data, year=2024).estimates == ()                # Other year: nothing borrowed.


def test_corrupted_acquisition_is_reported_and_the_others_are_kept(local_data):
    later = list_local_observations(local_data)[-1]
    (later.manifest_path.parent / "b04.tif").write_bytes(b"changed")
    result = inventory(local_data)
    assert [e.acquired_at_utc.day for e in result.estimates] == [15]
    (label, reason), = result.failures
    assert "S2B_TEST_25" in label and "impossible" in reason and str(local_data) not in reason


def test_insufficient_coverage_keeps_measurements_but_publishes_nothing(local_data):
    strict = replace(S2_NDVI_VEGETATION_V0, min_observed_coverage_percent=60.0)
    result = inventory(local_data, profile=strict)
    estimate = result.estimates[-1]
    assert not estimate.assessment.available and estimate.measure.observed_m2 > 0   # Diagnosis kept.
    indicator = build_s2_vegetation_indicator(estimate, result)
    assert indicator.status == UNAVAILABLE and indicator.value is None and indicator.shares == ()
    assert "60 %" in indicator.reliability_reasons[0] and result.default_estimate() is None


def test_resource_limits_refuse_before_processing(local_data, monkeypatch):
    import src.services.local_preview as local_preview

    monkeypatch.setattr(local_preview, "analyze_local_group_with_scl",
                        lambda *a, **k: pytest.fail("Aucun calcul ne doit être lancé."))
    result = inventory(local_data, limits=ResourceLimits(warn_cells=1, max_cells=2))
    assert result.estimates == () and any("refusé avant traitement" in note for note in result.notes)


# --- Multi-tile mosaic ---------------------------------------------------------------------------------------


def write_product(root, product_id, x0, acquired_at):
    folder = root / "raw" / "sentinel2" / product_id
    folder.mkdir(parents=True)
    paths = {}
    for role, values in (("b04", np.full((2, 2), 2000, "uint16")), ("b08", np.full((2, 2), 5000, "uint16")),
                         ("scl", np.array([[4, 4], [9, 4]], "uint8"))):
        paths[role] = folder / f"{role}.tif"
        with rasterio.open(paths[role], "w", driver="GTiff", width=2, height=2, count=1, dtype=values.dtype,
                           crs="EPSG:32632", transform=from_origin(x0, 100000, 10, 10), nodata=0) as target:
            target.write(values, 1)
    paths["calibration_xml"] = folder / "metadata.xml"
    paths["calibration_xml"].write_text(
        f"<Product><PRODUCT_URI>{product_id}.SAFE</PRODUCT_URI><BOA_QUANTIFICATION_VALUE>10000"
        "</BOA_QUANTIFICATION_VALUE><BOA_ADD_OFFSET band_id=\"3\">-1000</BOA_ADD_OFFSET>"
        "<BOA_ADD_OFFSET band_id=\"7\">-1000</BOA_ADD_OFFSET></Product>", encoding="utf-8")
    manifest = build_acquisition_manifest(
        product={"id": product_id, "collection": "sentinel-2-l2a", "properties": {"datetime": acquired_at},
                 "assets": {role: {"href": f"s3://test/{role}"} for role in paths}},
        asset_paths=paths, asset_ids={role: role for role in paths}, stac_source="https://example.org/stac",
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc), product_directory=folder,
        integrity={"mode": "strict", "evidence": {}, "source_assets": {
            role: {"status": "verified", "method": "stac_file_checksum", "algorithm": "sha256",
                   "declared_checksum_status": "valid"} for role in paths}})
    write_acquisition_manifest(manifest, folder / "acquisition_manifest.json")


def test_mosaic_keeps_main_figures_without_detailed_breakdown(tmp_path):
    # One datatake (orbit R136, same instant) over two MGRS tiles side by side.
    write_product(tmp_path, "S2B_MSIL2A_20250125T093159_N0511_R136_T32NNF_20250125T115658", 500000,
                  "2025-01-25T09:31:59Z")
    write_product(tmp_path, "S2B_MSIL2A_20250125T093159_N0511_R136_T32NPF_20250125T115658", 500020,
                  "2025-01-25T09:31:59Z")
    (tmp_path / "aoi").mkdir()
    gpd.GeoDataFrame(geometry=[box(500000, 99980, 500040, 100000)], crs="EPSG:32632").to_file(
        tmp_path / "aoi" / "akanda.geojson")
    result = inventory(tmp_path)
    (estimate,) = result.estimates
    m = estimate.measure
    assert estimate.breakdown_status == BREAKDOWN_UNAVAILABLE and len(estimate.product_ids) == 2
    assert m.not_observed_m2 is None and m.excluded_observed_m2 is None and m.unsplit_m2 > 0   # Cloud class 9.
    assert m.observed_coverage_percent == pytest.approx(75.0, rel=1e-3) and m.vegetated_share_percent == 100.0
    indicator = build_s2_vegetation_indicator(estimate, result)
    assert indicator.status == AVAILABLE and indicator.value == pytest.approx(m.vegetated_m2 / 1e6)
    assert any("Ventilation non disponible" in limit for limit in indicator.limits)
    assert "unsplit" in [item.key for item in indicator.breakdown]


def test_manifest_dates_with_z_suffix_are_read():
    assert parse_utc("2026-10-16T17:10:20.485430Z") == datetime(2026, 10, 16, 17, 10, 20, 485430, tzinfo=timezone.utc)


# --- Pack v3 reference values (reproducibility, not scientific validation) ------------------------------------

PACK_REFERENCE = {   # Computed on 28/10/2026 (Windows, Python 3.10); areas in m².
    "2025-01-15": dict(observed_m2=96157195.72065385, vegetated_m2=68922168.1187398,
                       not_vegetated_m2=27235027.601914037, excluded_observed_m2=24985507.720125146,
                       not_observed_m2=22965862.5656221, observed_pixels=962307, vegetated_pixels=690098,
                       sensitivity=(79294855.49110602, 68922168.1187398, 54746003.28834883), available=True),
    "2025-01-20": dict(observed_m2=3918042.92119911, vegetated_m2=853037.0190666369,
                       not_vegetated_m2=3065005.902132473, excluded_observed_m2=17825877.061961595,
                       not_observed_m2=122364646.0232404, observed_pixels=39160, vegetated_pixels=8525,
                       sensitivity=(1757839.7539259794, 853037.0190666369, 252089.4255767077), available=False),
    "2025-01-25": dict(observed_m2=92967369.73900154, vegetated_m2=61614463.77370939,
                       not_vegetated_m2=31352905.96529216, excluded_observed_m2=17838064.45455369,
                       not_observed_m2=33303131.812845834, observed_pixels=930515, vegetated_pixels=617081,
                       sensitivity=(72621590.54798701, 61614463.77370939, 47151712.65545003), available=True),
}
PACK_ZONE_M2 = 144108566.0064011


@pytest.fixture(scope="module")
def pack_inventory():
    if not (PACK / "PACK_MANIFEST.json").is_file():
        pytest.skip("Paquet de démonstration absent.")
    from src.data.demo_pack import verify_pack

    verify_pack(PACK)
    return s2_vegetation_inventory(PACK, "akanda", available_bytes=None)


def test_pack_v3_three_acquisitions_are_reproduced(pack_inventory):
    assert not pack_inventory.failures and not pack_inventory.notes
    found = {e.acquired_at_utc.date().isoformat(): e for e in pack_inventory.estimates}
    assert set(found) == set(PACK_REFERENCE)
    for day, expected in PACK_REFERENCE.items():
        estimate, m = found[day], found[day].measure
        assert estimate.breakdown_status == BREAKDOWN_AVAILABLE and estimate.assessment.available is expected["available"]
        assert m.zone_m2 == pytest.approx(PACK_ZONE_M2, rel=AREA_RTOL)
        for name in ("observed_m2", "vegetated_m2", "not_vegetated_m2", "excluded_observed_m2", "not_observed_m2"):
            assert getattr(m, name) == pytest.approx(expected[name], rel=AREA_RTOL, abs=0), (day, name)
        assert (m.observed_pixels, m.vegetated_pixels) == (expected["observed_pixels"], expected["vegetated_pixels"])
        assert [t for t, _ in m.sensitivity_m2] == [0.3, 0.4, 0.5]
        for (_, area), reference in zip(m.sensitivity_m2, expected["sensitivity"]):
            assert area == pytest.approx(reference, rel=AREA_RTOL, abs=0), day
        assert m.other_invalid_m2 == 0 and m.outside_footprint_m2 == 0
    assert pack_inventory.default_estimate() is found["2025-01-25"]          # 20/01 below 30 %: not the default.
    assert not holds_arrays(pack_inventory)


def test_pack_v3_observed_area_is_the_one_of_the_existing_chain(pack_inventory):
    observation = next(o for o in list_local_observations(PACK) if o.acquired_at.day == 25)
    reference = analyze_local_observation(observation, PACK, scientific_profile=AKANDA)
    estimate = pack_inventory.estimates[-1]
    assert estimate.measure.observed_m2 == reference.valid_area_m2          # Bit for bit.
    assert estimate.measure.observed_pixels == reference.summary.count == 930515
