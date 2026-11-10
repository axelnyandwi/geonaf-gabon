"""Réutilisation des poids d'aire entre paires, sur une configuration réaliste synthétique.

Reproduit la situation des tuiles Sentinel-2 locales sans en dépendre : SCL à 20 m en
UTM 32N (EPSG:32632), AOI GeoJSON en WGS84 rognée dans la tuile, et une troisième
acquisition logique qui partage le fichier SCL de la première. Les rasters et AOI sont
générés dans ``tmp_path`` avec une graine fixe : aucun fichier de ``data/`` n'est lu.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import MultiPolygon, box

import src.geo.pair_selection as selector


SCL_CRS = "EPSG:32632"
SCL_RESOLUTION = 20.0
SCL_SIZE = 120  # 2,4 km de côté : assez grand pour que l'AOI soit rognée.
SCL_ORIGIN = (400000.0, 50000.0)  # Coin nord-ouest, latitude ≈ 0,45° N (Estuaire).
# Classes SCL rencontrées sur les tuiles réelles, y compris nodata (0), nuages et eau.
SCL_CLASSES = np.array([0, 3, 4, 5, 6, 7, 8, 9, 10], dtype=np.uint8)

# AOI exprimées en mètres UTM puis reprojetées en WGS84 ; bords non alignés sur les pixels.
X0, Y0 = SCL_ORIGIN
AOI_CASES = {
    "interieure": box(X0 + 310, Y0 - 1990, X0 + 1730, Y0 - 570),
    "debordante": box(X0 - 500, Y0 - 1210, X0 + 1130, Y0 + 400),
    "multipartie": MultiPolygon(
        [
            box(X0 + 115, Y0 - 905, X0 + 795, Y0 - 225),
            box(X0 + 1405, Y0 - 2085, X0 + 2195, Y0 - 1295),
        ]
    ),
}


def _write_scl(path, seed):
    values = np.random.default_rng(seed).choice(SCL_CLASSES, size=(SCL_SIZE, SCL_SIZE))
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=SCL_SIZE,
        height=SCL_SIZE,
        count=1,
        dtype="uint8",
        crs=SCL_CRS,
        transform=from_origin(X0, Y0, SCL_RESOLUTION, SCL_RESOLUTION),
        nodata=0,
    ) as dataset:
        dataset.write(values, 1)
    return path


def _write_aoi(path, geometry):
    gpd.GeoDataFrame({"geometry": [geometry]}, crs=SCL_CRS).to_crs("EPSG:4326").to_file(
        path, driver="GeoJSON"
    )
    return path


@pytest.mark.parametrize("aoi_name", sorted(AOI_CASES))
def test_area_weights_reused_between_pairs(tmp_path, aoi_name):
    aoi_path = _write_aoi(tmp_path / f"{aoi_name}.geojson", AOI_CASES[aoi_name])

    scl_15 = _write_scl(tmp_path / "T32NNF_20250115_SCL_20m.tif", seed=15)
    scl_25 = _write_scl(tmp_path / "T32NNF_20250125_SCL_20m.tif", seed=25)

    # Trois acquisitions logiques, deux fichiers SCL.
    # Le troisième produit réutilise le fichier du premier.
    acquisitions = [
        ("TEST_15", scl_15, 0),
        ("TEST_25", scl_25, 10),
        ("TEST_35_SYNTHETIC", scl_15, 20),
    ]

    start_date = datetime(2025, 1, 15, tzinfo=timezone.utc)

    search_results = []
    scl_paths = {}
    quality_by_product = {}

    for product_id, path, offset_days in acquisitions:
        acquired_at = start_date + timedelta(days=offset_days)

        search_results.append({
            "id": product_id,
            "properties": {
                "datetime": acquired_at.isoformat(),
                "eo:cloud_cover": 30.0,
            },
        })

        scl_paths[product_id] = path

        # Valeurs fictives, uniquement pour les préfiltres.
        quality_by_product[product_id] = {
            "usable_land_percent": 100.0,
            "aoi_coverage_percent": 100.0,
            "aoi_data_coverage_percent": 100.0,
        }

    original_weights = selector.prepare_pixel_area_weights
    original_grid_key = selector._grid_cache_key

    grid_keys = []
    weight_calls = []

    def tracked_grid_key(grid):
        key = original_grid_key(grid)
        grid_keys.append(key)
        return key

    def tracked_weights(grid, aoi):
        weight_calls.append(original_grid_key(grid))
        return original_weights(grid, aoi)

    with (
        patch.object(selector, "_grid_cache_key", side_effect=tracked_grid_key),
        patch.object(selector, "prepare_pixel_area_weights", side_effect=tracked_weights),
    ):
        result = selector.select_temporal_pairs(
            search_results,
            scl_paths=scl_paths,
            quality_by_product=quality_by_product,
            aoi_path=aoi_path,
            config=selector.PairSelectionConfig(max_pair_evaluations=3),
            search_scope=selector.PairSearchScope.LOCAL_SUBSET,
        )

    # Les trois combinaisons doivent utiliser une grille commune identique,
    # rognée sur l'AOI et non sur la tuile entière.
    assert len(grid_keys) == 3
    assert len(set(grid_keys)) == 1
    crs, _, width, height = grid_keys[0]
    assert rasterio.crs.CRS.from_string(crs) == rasterio.crs.CRS.from_string(SCL_CRS)
    assert 0 < width < SCL_SIZE and 0 < height < SCL_SIZE

    # Un seul calcul de poids pour les trois paires.
    assert len(weight_calls) == 1
    assert weight_calls[0] == grid_keys[0]

    assert result.evaluated_pairs == 3
    assert len(result.pairs) == 3
    assert not result.raster_failures
