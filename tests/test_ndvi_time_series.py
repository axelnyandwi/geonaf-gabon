"""NDVI series contracts on synthetic verified products; not ecological validation."""

from dataclasses import replace
from datetime import date, datetime, timezone
import csv
import io
import json

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from src.data.acquisition_manifest import build_acquisition_manifest, write_acquisition_manifest
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL, ScientificStatus, SeasonalRules
from src.geo.temporal import build_common_grid, build_series_grid
from src.services.local_preview import (
    LocalPreviewError, analyze_local_akanda, analyze_local_ndvi_series, list_local_observations,
    local_series_key,
)
from src.services.ndvi_time_series import analyze_ndvi_time_series, series_csv

PERIOD = (date(2025, 1, 1), date(2025, 2, 28))
# Date -> (B04 DN, B08 DN, SCL). 4/5 usable land; 9 cloud; 2 shadow.
SCENES = {
    "2025-01-05": (np.array([[1800, 1900, 2000], [2100, 2200, 2300], [2400, 2500, 2600]]),
                   np.array([[5000, 5100, 5200], [5300, 5400, 5500], [5600, 5700, 5800]]),
                   np.array([[4, 4, 5], [4, 9, 5], [4, 4, 4]])),
    "2025-01-15": (np.array([[2000, 2000, 2000], [2000, 2000, 2000], [2000, 2000, 2000]]),
                   np.array([[4800, 4900, 5000], [5100, 5200, 5300], [5400, 5500, 5600]]),
                   np.array([[4, 4, 5], [2, 4, 5], [4, 4, 4]])),
    "2025-01-20": (np.full((3, 3), 3000), np.full((3, 3), 3500),
                   np.full((3, 3), 9)),
    "2025-02-04": (np.array([[2200, 2100, 2000], [1900, 1800, 1700], [1600, 1500, 1400]]),
                   np.array([[4000, 4100, 4200], [4300, 4400, 4500], [4600, 4700, 4800]]),
                   np.array([[4, 5, 5], [4, 4, 4], [4, 4, 9]])),
}


def _product(root, day, b04, b08, scl):
    folder = root / "raw" / "sentinel2" / f"product-{day}"
    folder.mkdir(parents=True)
    product_id = f"S2B_SERIES_{day.replace('-', '')}"
    paths = {}
    for role, values in (("b04", b04.astype("uint16")), ("b08", b08.astype("uint16")),
                         ("scl", scl.astype("uint8"))):
        paths[role] = folder / f"{role}.tif"
        with rasterio.open(paths[role], "w", driver="GTiff", width=3, height=3, count=1,
                           dtype=values.dtype, crs="EPSG:32632",
                           transform=from_origin(500000, 100000, 10, 10), nodata=0) as dst:
            dst.write(values, 1)
    paths["calibration_xml"] = folder / "metadata.xml"
    paths["calibration_xml"].write_text(f'''<Product><PRODUCT_URI>{product_id}.SAFE</PRODUCT_URI>
    <BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>
    <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
    <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET></Product>''', encoding="utf-8")
    manifest = build_acquisition_manifest(
        product={"id": product_id, "collection": "sentinel-2-l2a",
                 "properties": {"datetime": f"{day}T09:30:00Z"},
                 "assets": {role: {"href": f"s3://test/{role}"} for role in paths}},
        asset_paths=paths, asset_ids={role: role for role in paths}, stac_source="https://example.org/stac",
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc), product_directory=folder,
        integrity={"mode": "strict", "evidence": {}, "source_assets": {
            role: {"status": "verified", "method": "stac_file_checksum", "algorithm": "sha256",
                   "declared_checksum_status": "valid"} for role in paths}},
    )
    write_acquisition_manifest(manifest, folder / "acquisition_manifest.json")


@pytest.fixture
def series_data(tmp_path):
    for day, scene in SCENES.items():
        _product(tmp_path, day, *scene)
    (tmp_path / "aoi").mkdir()
    gpd.GeoDataFrame(geometry=[box(500000, 99970, 500030, 100000)],
                     crs="EPSG:32632").to_file(tmp_path / "aoi" / "akanda.geojson")
    return tmp_path


def _series(data, observations=None, profile=GABON_EQUATORIAL_EXPERIMENTAL, **kwargs):
    observations = observations or list_local_observations(data)
    return analyze_local_ndvi_series(observations, data, scientific_profile=profile,
                                     period=kwargs.pop("period", PERIOD), **kwargs)


def _expected_ndvi(b04, b08):
    red, nir = (b04 - 1000) / 10000, (b08 - 1000) / 10000
    return (nir - red) / (nir + red)


def test_series_grid_matches_pair_grid(tmp_path):
    paths = []
    for index, origin in enumerate((500000, 500010, 500020)):
        path = tmp_path / f"{index}.tif"
        with rasterio.open(path, "w", driver="GTiff", width=5, height=4, count=1, dtype="float32",
                           crs="EPSG:32632", transform=from_origin(origin, 100000, 10, 10)) as dst:
            dst.write(np.zeros((4, 5), "float32"), 1)
        paths.append(path)
    assert build_series_grid(paths[:2]) == build_common_grid(*paths[:2])
    grid = build_series_grid(paths)
    assert (grid.width, grid.height, grid.transform.c) == (3, 4, 500020)
    with pytest.raises(ValueError):
        build_series_grid(paths[:1])


def test_own_and_common_statistics_are_separate_and_cloudy_date_is_excluded(series_data):
    result = _series(series_data)
    by_day = {item.acquired_at_utc.date().isoformat(): item for item in result.dates}
    cloudy = by_day["2025-01-20"]
    assert not cloudy.included and "Couverture valide" in cloudy.exclusion_reason
    assert cloudy.own.count == 0 and cloudy.own.mean is None and cloudy.common is None
    # Common pixels: usable on 05, 15 and 04/02 (cloud 9 and shadow 2 excluded).
    usable = np.ones((3, 3), bool)
    for day in ("2025-01-05", "2025-01-15", "2025-02-04"):
        usable &= np.isin(SCENES[day][2], (4, 5))
    assert result.common_pixel_count == usable.sum() == 6
    for day in ("2025-01-05", "2025-01-15", "2025-02-04"):
        b04, b08, scl = SCENES[day]
        ndvi = _expected_ndvi(b04, b08)
        assert by_day[day].common.mean == pytest.approx(ndvi[usable].mean(), abs=1e-6)
        assert by_day[day].own.mean == pytest.approx(ndvi[np.isin(scl, (4, 5))].mean(), abs=1e-6)
        assert by_day[day].own.count == np.isin(scl, (4, 5)).sum()
    assert result.common_coverage_percent == pytest.approx(600 / 9, abs=1e-6)
    assert result.interpretation.status != ScientificStatus.VALIDATED


def test_two_retained_dates_reproduce_pair_pipeline_pixelwise(series_data):
    observations = list_local_observations(series_data)
    first, second, cloudy = observations[0], observations[1], observations[2]
    pair = analyze_local_akanda(first, second, series_data, scientific_profile=GABON_EQUATORIAL_EXPERIMENTAL)
    # The cloudy third date is excluded: the common set is exactly the pair's comparable set.
    result = _series(series_data, [first, second, cloudy])
    assert result.grid == pair.common_grid
    mask = pair.analysis.comparable_mask
    assert result.common_pixel_count == int(mask.sum()) == pair.analysis.statistics["count"]
    assert result.common_coverage_percent == pair.analysis.comparable_coverage_percent
    retained = result.included_dates
    assert retained[0].common.mean == pytest.approx(float(np.mean(pair.comparison.ndvi_date_1[mask], dtype=np.float64)), abs=1e-12)
    assert retained[1].common.mean == pytest.approx(float(np.mean(pair.comparison.ndvi_date_2[mask], dtype=np.float64)), abs=1e-12)
    assert retained[1].common.mean - retained[0].common.mean == pytest.approx(pair.analysis.statistics["mean"], abs=1e-6)
    assert result.interpretation.status == ScientificStatus.NOT_COMPARABLE  # Only two retained dates.


def test_status_rules_never_validate(series_data):
    result = _series(series_data)
    assert result.interpretation.status == ScientificStatus.NOT_ASSESSED  # No calendar in V1 profile.
    assert any("pixels différents" in text for text in result.interpretation.restrictions)
    calendar = SeasonalRules(month_seasons=("A",) * 1 + ("B",) * 11, source="test", version="1")
    seasonal = replace(GABON_EQUATORIAL_EXPERIMENTAL, seasonal_rules=calendar)
    differing = _series(series_data, profile=seasonal)
    assert differing.interpretation.status == ScientificStatus.NOT_COMPARABLE
    same = replace(GABON_EQUATORIAL_EXPERIMENTAL,
                   seasonal_rules=SeasonalRules(month_seasons=("A",) * 12, source="test", version="1"))
    assert _series(series_data, profile=same).interpretation.status == ScientificStatus.EXPLORATION
    strict = replace(GABON_EQUATORIAL_EXPERIMENTAL, min_comparable_coverage_percent=80.0)
    assert _series(series_data, profile=strict).interpretation.status == ScientificStatus.NOT_COMPARABLE


def test_gaps_are_reported_against_period_and_threshold(series_data):
    result = _series(series_data, gap_warning_days=15)
    kinds = [(gap.kind, round(gap.days, 3), gap.exceeds_warning) for gap in result.gaps]
    # Retained: 05/01, 15/01, 04/02 (20/01 excluded).
    assert kinds[1:3] == [("between_dates", 10.0, False), ("between_dates", 20.0, True)]
    assert kinds[0][0] == "period_start" and kinds[-1][0] == "period_end" and kinds[-1][2]
    assert any("Lacunes" in text for text in result.interpretation.restrictions)


def test_inputs_are_validated(series_data):
    observations = list_local_observations(series_data)
    with pytest.raises(LocalPreviewError):
        _series(series_data, observations[:2])
    with pytest.raises(LocalPreviewError):
        _series(series_data, period=(date(2025, 1, 10), date(2025, 1, 31)))
    with pytest.raises(LocalPreviewError):
        _series(series_data, gap_warning_days=0)
    acquisitions = []
    from src.services.local_preview import _load_observation
    acquisitions = [_load_observation(item) for item in observations[:3]]
    with pytest.raises(ValueError, match="distinctes"):
        analyze_ndvi_time_series(acquisitions + [acquisitions[0]], aoi_path=series_data / "aoi/akanda.geojson",
                                 scientific_profile=GABON_EQUATORIAL_EXPERIMENTAL)


def test_tampered_asset_fails_without_paths_and_key_changes(series_data):
    observations = list_local_observations(series_data)
    key = local_series_key(observations, series_data, GABON_EQUATORIAL_EXPERIMENTAL, PERIOD, 15)
    path = observations[1].manifest_path.parent / "b04.tif"
    path.write_bytes(path.read_bytes() + b"x")
    assert local_series_key(observations, series_data, GABON_EQUATORIAL_EXPERIMENTAL, PERIOD, 15) != key
    with pytest.raises(LocalPreviewError) as error:
        _series(series_data, observations)
    assert str(series_data) not in str(error.value)


def test_csv_export_has_provenance_and_no_paths(series_data):
    result = _series(series_data)
    text = series_csv(result)
    assert str(series_data) not in text and "\\" not in text
    header = dict(line[2:].split(": ", 1) for line in text.splitlines() if line.startswith("# "))
    assert header["profile_sha256"] == GABON_EQUATORIAL_EXPERIMENTAL.fingerprint
    assert header["status"] == result.interpretation.status.value
    assert header["period_utc"] == "2025-01-01/2025-02-28" and header["scl_policy"] == "usable_land"
    rows = list(csv.DictReader(io.StringIO("\n".join(l for l in text.splitlines() if not l.startswith("#")))))
    assert len(rows) == 4 and rows[2]["included"] == "False" and rows[2]["common_mean"] == ""
    manifest = json.loads(list_local_observations(series_data)[0].manifest_path.read_text())
    assert rows[0]["b04_sha256"] == manifest["assets"]["b04"]["sha256"]
