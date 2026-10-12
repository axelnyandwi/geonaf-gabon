"""Tests synthétiques de qualité SCL et de classement local des produits."""

import shutil

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from src.data import quality


def _write_scl(
    path,
    values,
    *,
    transform=None,
    crs="EPSG:32632",
    nodata=0,
    valid_mask=None,
):
    values = np.asarray(values, dtype=np.uint8)
    if transform is None:
        transform = from_origin(0, 40, 10, 10)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=values.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as destination:
        destination.write(values, 1)
        if valid_mask is not None:
            destination.write_mask(np.asarray(valid_mask, dtype=np.uint8) * 255)
    return path


def _product(product_id, cloud_cover=50):
    return {
        "id": product_id,
        "properties": {"eo:cloud_cover": cloud_cover},
        "assets": {"SCL_20m": {"href": f"synthetic://{product_id}/SCL_20m.tif"}},
    }


def _patch_local_pipeline(monkeypatch, raster_by_href):
    """Remplace téléchargement et découpage par une copie locale synthétique."""
    def fake_download(*, s3_client, asset, destination_dir):
        return raster_by_href[asset["href"]]

    def fake_clip(*, input_path, output_path, aoi_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(input_path, output_path)
        return output_path

    monkeypatch.setattr(quality, "download_asset", fake_download)
    monkeypatch.setattr(quality, "clip_raster_to_aoi", fake_clip)


def _select(items, tmp_path):
    aoi_path = tmp_path / "synthetic-aoi.geojson"
    gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 20), (20, 20), (20, 40), (0, 40)])]},
        geometry="geometry",
        crs="EPSG:32632",
    ).to_file(aoi_path, driver="GeoJSON")
    return quality.select_best_product_for_aoi(
        items=items,
        aoi_path=aoi_path,
        s3_client=object(),
        raw_dir=tmp_path / "raw",
        interim_dir=tmp_path / "interim",
    )


def test_fully_reliable_scene_scores_one_hundred_percent(tmp_path):
    """Une scène entièrement classée végétation/sol nu est 100 % exploitable."""
    scl_path = _write_scl(tmp_path / "all-reliable.tif", [[4, 5], [5, 4]])

    result = quality.calculate_scl_quality(scl_path)

    assert result["total_pixels"] == 4
    assert result["land_pixels"] == 4
    assert result["usable_land_pixels"] == 4
    assert result["usable_land_percent"] == 100.0
    assert result["invalid_percent"] == 0.0
    assert result["aoi_coverage_percent"] is None
    assert result["aoi_data_coverage_percent"] is None


def test_aoi_fully_covered_has_full_geographic_and_data_coverage(tmp_path):
    """Sépare la présence de l’emprise raster de la validité de ses pixels SCL."""
    scl_path = _write_scl(
        tmp_path / "full.tif", np.full((4, 4), 4, dtype=np.uint8)
    )
    aoi_path = tmp_path / "full-aoi.geojson"
    gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 0), (40, 0), (40, 40), (0, 40)])]},
        geometry="geometry",
        crs="EPSG:32632",
    ).to_file(aoi_path, driver="GeoJSON")

    result = quality.calculate_scl_quality(
        scl_path,
        aoi_path=aoi_path,
        coverage_raster_path=scl_path,
    )

    assert result["aoi_coverage_percent"] == pytest.approx(100.0)
    assert result["aoi_data_coverage_percent"] == pytest.approx(100.0)
    assert result["usable_land_percent"] == 100.0


def test_aoi_partially_covered_reports_surface_fraction(tmp_path):
    """Le dénominateur est toute l’AOI, y compris sa partie hors emprise."""
    scl_path = _write_scl(
        tmp_path / "partial.tif", np.full((4, 4), 4, dtype=np.uint8)
    )
    aoi_path = tmp_path / "partial.geojson"
    gpd.GeoDataFrame(
        {"geometry": [Polygon([(30, 10), (50, 10), (50, 30), (30, 30)])]},
        geometry="geometry",
        crs="EPSG:32632",
    ).to_file(aoi_path, driver="GeoJSON")

    result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_path, coverage_raster_path=scl_path
    )

    assert result["aoi_coverage_percent"] == pytest.approx(50.0, abs=0.01)
    assert result["aoi_data_coverage_percent"] == pytest.approx(50.0, abs=0.01)


def test_aoi_without_intersection_has_zero_coverage(tmp_path):
    """Une scène distante a une couverture géographique et de données nulle."""
    scl_path = _write_scl(
        tmp_path / "remote.tif", np.full((4, 4), 4, dtype=np.uint8)
    )
    aoi_path = tmp_path / "remote-aoi.geojson"
    gpd.GeoDataFrame(
        {"geometry": [Polygon([(50, 10), (70, 10), (70, 30), (50, 30)])]},
        geometry="geometry",
        crs="EPSG:32632",
    ).to_file(aoi_path, driver="GeoJSON")

    result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_path, coverage_raster_path=scl_path
    )

    assert result["aoi_coverage_percent"] == 0.0
    assert result["aoi_data_coverage_percent"] == 0.0


def test_multipart_overlapping_aoi_features_are_unioned_before_area(tmp_path):
    """Les parties superposées ne gonflent pas le dénominateur surfacique."""
    scl_path = _write_scl(
        tmp_path / "multipart.tif", np.full((4, 4), 4, dtype=np.uint8)
    )
    aoi_path = tmp_path / "multipart.geojson"
    polygons = [
        Polygon([(0, 0), (20, 0), (20, 20), (0, 20)]),
        Polygon([(10, 0), (30, 0), (30, 20), (10, 20)]),
        Polygon([(50, 0), (70, 0), (70, 20), (50, 20)]),
    ]
    gpd.GeoDataFrame(
        {"geometry": polygons}, geometry="geometry", crs="EPSG:32632"
    ).to_file(aoi_path, driver="GeoJSON")

    result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_path, coverage_raster_path=scl_path
    )

    assert result["aoi_coverage_percent"] == pytest.approx(60.0, abs=0.01)
    assert result["aoi_data_coverage_percent"] == pytest.approx(60.0, abs=0.01)


def test_class_zero_and_raster_mask_reduce_data_coverage(tmp_path):
    """Classe 0 et masque dataset sont des trous de données, pas de l’eau."""
    values = np.full((4, 4), 4, dtype=np.uint8)
    values[0, 0] = 0
    valid_mask = np.ones((4, 4), dtype=np.uint8)
    valid_mask[0, 1] = 0
    scl_path = _write_scl(
        tmp_path / "masked.tif", values, valid_mask=valid_mask
    )
    aoi_path = tmp_path / "masked-aoi.geojson"
    gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 0), (40, 0), (40, 40), (0, 40)])]},
        geometry="geometry",
        crs="EPSG:32632",
    ).to_file(aoi_path, driver="GeoJSON")

    result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_path, coverage_raster_path=scl_path
    )

    assert result["aoi_coverage_percent"] == pytest.approx(100.0)
    assert result["aoi_data_coverage_percent"] == pytest.approx(87.5)


def test_dataset_mask_excludes_pixels_from_scl_quality_denominator(tmp_path):
    """Masked nonzero cloudy pixels affect neither counts nor score denominators."""
    values = np.full((2, 2), 4, dtype=np.uint8)
    values[0, 0] = 9
    valid_mask = np.ones((2, 2), dtype=bool)
    valid_mask[0, 0] = False
    scl_path = _write_scl(
        tmp_path / "masked-cloud.tif",
        values,
        valid_mask=valid_mask,
    )

    result = quality.calculate_scl_quality(scl_path)

    assert result["total_pixels"] == 3
    assert result["land_pixels"] == 3
    assert result["usable_land_percent"] == pytest.approx(100.0)
    assert result["invalid_percent"] == 0.0


def test_masked_scl_values_are_excluded_from_every_quality_indicator(tmp_path):
    """A masked nonzero class is not water, land, invalid, or observed data."""
    values = np.array([[4, 8, 6, 0], [5, 7, 3, 9]], dtype=np.uint8)
    valid_mask = np.ones(values.shape, dtype=bool)
    valid_mask[0, 1:3] = False  # Masked cloud and water classes.
    scl_path = _write_scl(
        tmp_path / "masked-classes.tif",
        values,
        valid_mask=valid_mask,
    )

    result = quality.calculate_scl_quality(scl_path)

    assert result["total_pixels"] == 5  # excludes masked cells and class 0
    assert result["land_pixels"] == 5
    assert result["usable_land_pixels"] == 2
    assert result["usable_land_percent"] == pytest.approx(40.0)
    assert result["uncertain_percent"] == pytest.approx(20.0)
    assert result["cloud_shadow_percent"] == pytest.approx(40.0)
    assert result["water_percent"] == 0.0
    assert result["invalid_percent"] == pytest.approx(40.0)


def test_water_is_valid_data_coverage_but_excluded_from_land_score(tmp_path):
    """L’eau compte comme observée, mais reste exclue du dénominateur terrestre."""
    values = np.full((2, 2), 4, dtype=np.uint8)
    values[0, 0] = 6
    scl_path = _write_scl(tmp_path / "water.tif", values)
    aoi_path = tmp_path / "water-aoi.geojson"
    gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 20), (20, 20), (20, 40), (0, 40)])]},
        geometry="geometry",
        crs="EPSG:32632",
    ).to_file(aoi_path, driver="GeoJSON")

    result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_path, coverage_raster_path=scl_path
    )

    assert result["aoi_data_coverage_percent"] == pytest.approx(100.0)
    assert result["water_percent"] == pytest.approx(25.0)
    assert result["usable_land_percent"] == 100.0


def test_boundary_pixels_are_weighted_by_their_aoi_intersection_area(tmp_path):
    """Un pixel à moitié dans l’AOI ne compte que pour sa fraction surfacique."""
    scl_path = _write_scl(
        tmp_path / "boundary.tif",
        np.array([[4, 0]], dtype=np.uint8),
        transform=from_origin(0, 10, 10, 10),
    )
    aoi_path = tmp_path / "boundary-aoi.geojson"
    gpd.GeoDataFrame(
        {"geometry": [Polygon([(5, 0), (15, 0), (15, 10), (5, 10)])]},
        geometry="geometry",
        crs="EPSG:32632",
    ).to_file(aoi_path, driver="GeoJSON")

    result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_path, coverage_raster_path=scl_path
    )

    assert result["aoi_coverage_percent"] == pytest.approx(100.0)
    assert result["aoi_data_coverage_percent"] == pytest.approx(50.0)


def test_coverage_is_consistent_for_equivalent_aoi_crs(tmp_path):
    """La représentation UTM ou WGS84 de la même AOI donne la même aire."""
    scl_path = _write_scl(
        tmp_path / "crs.tif", np.full((4, 4), 4, dtype=np.uint8)
    )
    aoi_utm_path = tmp_path / "aoi-utm.geojson"
    aoi_wgs84_path = tmp_path / "aoi-wgs84.geojson"
    frame = gpd.GeoDataFrame(
        {"geometry": [Polygon([(30, 10), (50, 10), (50, 30), (30, 30)])]},
        geometry="geometry",
        crs="EPSG:32632",
    )
    frame.to_file(aoi_utm_path, driver="GeoJSON")
    frame.to_crs("EPSG:4326").to_file(aoi_wgs84_path, driver="GeoJSON")

    utm_result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_utm_path, coverage_raster_path=scl_path
    )
    wgs84_result = quality.calculate_scl_quality(
        scl_path, aoi_path=aoi_wgs84_path, coverage_raster_path=scl_path
    )

    assert wgs84_result["aoi_coverage_percent"] == pytest.approx(
        utm_result["aoi_coverage_percent"], abs=0.01
    )
    assert wgs84_result["aoi_data_coverage_percent"] == pytest.approx(
        utm_result["aoi_data_coverage_percent"], abs=0.01
    )


def test_cloudy_scene_scores_below_clear_scene_and_ranks_worse(
    tmp_path, monkeypatch
):
    """Les nuages/ombres diminuent le score local et le rang de l’acquisition."""
    clear = _write_scl(tmp_path / "clear.tif", [[4, 5], [4, 5]])
    cloudy = _write_scl(tmp_path / "cloudy.tif", [[4, 8], [9, 3]])
    items = [_product("CLOUDY", 5), _product("CLEAR", 80)]
    _patch_local_pipeline(
        monkeypatch,
        {
            items[0]["assets"]["SCL_20m"]["href"]: cloudy,
            items[1]["assets"]["SCL_20m"]["href"]: clear,
        },
    )

    result = _select(items, tmp_path)

    assert result["best_product_id"] == "CLEAR"
    assert [entry["product_id"] for entry in result["ranking"]] == [
        "CLEAR",
        "CLOUDY",
    ]
    assert result["ranking"][0]["usable_land_percent"] == 100.0
    assert result["ranking"][1]["usable_land_percent"] == 25.0
    assert result["ranking"][1]["cloud_shadow_percent"] == 75.0


def test_each_scl_class_has_expected_quality_category(tmp_path):
    """Un pixel par classe documente les catégories et leurs dénominateurs."""
    scl_path = _write_scl(
        tmp_path / "all-classes.tif",
        [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]],
    )

    result = quality.calculate_scl_quality(scl_path)

    assert result["total_pixels"] == 11  # classe 0 absente du dénominateur
    assert result["land_pixels"] == 10  # tout sauf eau (6) et NoData (0)
    assert result["usable_land_pixels"] == 2  # Classes fiables 4 et 5
    assert result["usable_land_percent"] == pytest.approx(20.0)
    assert result["uncertain_percent"] == pytest.approx(20.0)  # 2 et 7
    assert result["cloud_shadow_percent"] == pytest.approx(40.0)  # 3, 8-10
    assert result["water_percent"] == pytest.approx(100 / 11)
    assert result["invalid_percent"] == pytest.approx(600 / 11)  # 1, 3, 8-11


def test_scene_with_only_water_and_nodata_has_no_valid_land_pixels(tmp_path):
    """Eau et NoData sont exclus du dénominateur terrestre sans division par zéro."""
    scl_path = _write_scl(tmp_path / "no-land.tif", [[0, 6], [6, 0]])

    result = quality.calculate_scl_quality(scl_path)

    assert result["land_pixels"] == 0
    assert result["usable_land_pixels"] == 0
    assert result["usable_land_percent"] == 0.0
    assert result["uncertain_percent"] == 0.0
    assert result["cloud_shadow_percent"] == 0.0
    assert result["total_pixels"] == 2
    assert result["water_percent"] == 100.0
    assert result["invalid_percent"] == 0.0


def test_quality_read_error_is_not_hidden_by_calculation(tmp_path):
    """Un fichier illisible remonte comme erreur Rasterio, pas comme score nul."""
    bad_raster = tmp_path / "not-a-raster.tif"
    bad_raster.write_text("synthetic invalid raster", encoding="utf-8")

    with pytest.raises(rasterio.errors.RasterioIOError):
        quality.calculate_scl_quality(bad_raster)


def test_equal_scores_keep_input_order(tmp_path, monkeypatch):
    """Le tri stable rend le départage des scores égaux reproductible."""
    first = _write_scl(tmp_path / "first.tif", [[4, 5], [2, 7]])
    second = _write_scl(tmp_path / "second.tif", [[5, 4], [7, 2]])
    items = [_product("FIRST"), _product("SECOND")]
    _patch_local_pipeline(
        monkeypatch,
        {
            items[0]["assets"]["SCL_20m"]["href"]: first,
            items[1]["assets"]["SCL_20m"]["href"]: second,
        },
    )

    result = _select(items, tmp_path)

    assert [entry["usable_land_percent"] for entry in result["ranking"]] == [
        50.0,
        50.0,
    ]
    assert [entry["product_id"] for entry in result["ranking"]] == [
        "FIRST",
        "SECOND",
    ]
    assert result["best_product_id"] == "FIRST"


def test_partly_outside_aoi_scores_only_pixels_inside_scene(tmp_path, monkeypatch):
    """Une AOI débordante montre que le score ne mesure pas la couverture spatiale."""
    scl_path = _write_scl(tmp_path / "scene.tif", np.full((4, 4), 4, dtype=np.uint8))
    aoi_path = tmp_path / "partial-aoi.geojson"
    aoi = gpd.GeoDataFrame(
        {"geometry": [Polygon([(30, 10), (60, 10), (60, 30), (30, 30)])]},
        geometry="geometry",
        crs="EPSG:32632",
    )
    aoi.to_file(aoi_path, driver="GeoJSON")
    item = _product("PARTIAL_AOI")
    href = item["assets"]["SCL_20m"]["href"]
    monkeypatch.setattr(quality, "download_asset", lambda **_kwargs: scl_path)
    # Keep the production AOI clipping function for this integration test.

    result = quality.select_best_product_for_aoi(
        items=[item],
        aoi_path=aoi_path,
        s3_client=object(),
        raw_dir=tmp_path / "raw",
        interim_dir=tmp_path / "interim",
    )

    assert href.endswith("SCL_20m.tif")
    assert result["best_quality"]["usable_land_percent"] == 100.0
    assert result["best_quality"]["total_pixels"] == 2


def test_no_products_available_raises_value_error(tmp_path):
    """Une recherche sans acquisitions ne peut pas produire de classement."""
    with pytest.raises(ValueError, match="Aucun produit Sentinel-2"):
        _select([], tmp_path)


def test_download_failure_is_recorded_when_another_product_succeeds(
    tmp_path, monkeypatch, caplog
):
    """Un candidat écarté reste traçable dans le résultat et dans les logs."""
    good_raster = _write_scl(tmp_path / "good.tif", [[4, 5]])
    failed_item = _product("DOWNLOAD_FAILURE")
    good_item = _product("GOOD_PRODUCT")
    _patch_local_pipeline(
        monkeypatch,
        {good_item["assets"]["SCL_20m"]["href"]: good_raster},
    )

    def download_or_fail(*, asset, **_kwargs):
        if asset["href"] == failed_item["assets"]["SCL_20m"]["href"]:
            raise PermissionError("synthetic CDSE permission failure")
        return good_raster

    monkeypatch.setattr(quality, "download_asset", download_or_fail)

    result = _select([failed_item, good_item], tmp_path)

    assert result["best_product_id"] == "GOOD_PRODUCT"
    assert result["failures"] == [
        {
            "product_id": "DOWNLOAD_FAILURE",
            "error_type": "PermissionError",
            "message": "synthetic CDSE permission failure",
        }
    ]
    assert "Échec du traitement Sentinel-2" in caplog.text


def test_all_product_failures_keep_the_original_cause(tmp_path, monkeypatch):
    """L’échec global conserve un message détaillé et la cause Python initiale."""
    item = _product("DOWNLOAD_FAILURE")

    def fail_download(**_kwargs):
        raise PermissionError("synthetic CDSE permission failure")

    monkeypatch.setattr(quality, "download_asset", fail_download)

    with pytest.raises(RuntimeError, match="DOWNLOAD_FAILURE.*PermissionError") as error:
        _select([item], tmp_path)

    assert isinstance(error.value.__cause__, PermissionError)
    assert "synthetic CDSE permission failure" in str(error.value.__cause__)


def test_product_failure_hides_local_paths_from_result_log_and_cause(
    tmp_path, monkeypatch, caplog
):
    """La traçabilité d’un échec ne doit pas révéler les chemins de données."""
    item = _product("PATH_FAILURE")

    def fail_download(*, destination_dir, **_kwargs):
        raise PermissionError(f"Cannot access {destination_dir}: denied")

    monkeypatch.setattr(quality, "download_asset", fail_download)

    with pytest.raises(RuntimeError) as caught:
        _select([item], tmp_path)

    path_strings = (str(tmp_path), tmp_path.as_posix())
    assert all(path not in str(caught.value) for path in path_strings)
    assert all(path not in str(caught.value.__cause__) for path in path_strings)
    assert all(path not in caplog.text for path in path_strings)
    assert "denied" in str(caught.value.__cause__)
