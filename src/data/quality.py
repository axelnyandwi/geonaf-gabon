"""Contrôles de qualité des données à développer."""
import logging
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.errors import WindowError
from rasterio.features import geometry_window
from shapely import area, intersection, is_empty, is_valid, polygons, transform, union_all
from shapely.geometry import Polygon, mapping
from src.data.cloud_mask import (
    SCL_CLOUD_SHADOW_CLASSES,
    SCL_DEFECTIVE_CLASSES,
    SCL_NO_DATA_CLASSES,
    SCL_SNOW_ICE_CLASSES,
    SCL_UNCERTAIN_CLASSES,
    SCL_USABLE_LAND_CLASSES,
    SCL_WATER_CLASSES,
)
from src.data.ingestion import (
    download_asset,
    get_asset,
)
from src.data.preparation import clip_raster_to_aoi
from src.diagnostics import safe_call, safe_exception_message

logger = logging.getLogger(__name__)

INVALID_SCL_CLASSES = (
    SCL_NO_DATA_CLASSES
    | SCL_DEFECTIVE_CLASSES
    | SCL_CLOUD_SHADOW_CLASSES
    | SCL_SNOW_ICE_CLASSES
)


AREA_CRS = "EPSG:6933"
PIXEL_AREA_CHUNK_ROWS = 128


def _calculate_aoi_surface_coverage(
    raster_path: Path,
    aoi_path: Path,
) -> tuple[float, float]:
    """Return geometric and valid-SCL area coverage as percentages of an AOI.

    Areas are measured in the global equal-area CRS EPSG:6933. Cell overlaps
    are weighted by their actual AOI intersection area, so boundary pixels,
    multipart AOIs, and overlapping AOI features are handled consistently.
    """
    import geopandas as gpd

    aoi = safe_call(gpd.read_file, aoi_path, paths=(aoi_path,))
    if aoi.empty:
        raise ValueError("AOI vide.")
    if aoi.crs is None:
        raise ValueError("L'AOI ne possède pas de CRS.")
    if any(geometry is None or is_empty(geometry) for geometry in aoi.geometry):
        raise ValueError("L'AOI contient une géométrie vide.")
    if not all(is_valid(geometry) for geometry in aoi.geometry):
        raise ValueError("L'AOI contient une géométrie invalide.")

    # Union both before measuring and before rasterizing/weighting, so
    # overlapping features never count the same AOI area more than once.
    aoi_area_geometry = union_all(aoi.to_crs(AREA_CRS).geometry.array)
    aoi_area = float(area(aoi_area_geometry))
    if not np.isfinite(aoi_area) or aoi_area <= 0:
        raise ValueError("L'aire de l'AOI est nulle ou invalide.")

    with safe_call(rasterio.open, raster_path, paths=(raster_path,)) as src:
        if src.crs is None:
            raise ValueError("CRS du raster SCL manquant.")

        aoi_in_raster_crs = union_all(
            aoi.to_crs(src.crs).geometry.array
        )
        area_transformer = Transformer.from_crs(
            src.crs,
            AREA_CRS,
            always_xy=True,
        )

        raster_corners = [
            src.transform * (0, 0),
            src.transform * (src.width, 0),
            src.transform * (src.width, src.height),
            src.transform * (0, src.height),
        ]
        raster_footprint = Polygon(raster_corners)
        footprint_in_area_crs = transform(
            raster_footprint,
            area_transformer.transform,
            interleaved=False,
        )
        covered_aoi_geometry = intersection(
            aoi_area_geometry,
            footprint_in_area_crs,
        )
        covered_area = float(area(covered_aoi_geometry))
        aoi_coverage_percent = np.clip(
            covered_area / aoi_area * 100,
            0.0,
            100.0,
        )

        try:
            window = geometry_window(
                src,
                [mapping(aoi_in_raster_crs)],
                boundless=False,
            )
        except WindowError:
            return float(aoi_coverage_percent), 0.0

        scl = src.read(1, window=window, masked=True)
        valid_data = (
            (scl.data != 0)
            & ~np.ma.getmaskarray(scl)
        )
        height, width = scl.shape
        window_transform = src.window_transform(window)
        valid_area = 0.0

        # Process bounded row strips to limit memory on larger AOIs.
        for row_start in range(0, height, PIXEL_AREA_CHUNK_ROWS):
            row_end = min(row_start + PIXEL_AREA_CHUNK_ROWS, height)
            rows, cols = np.meshgrid(
                np.arange(row_start, row_end, dtype=np.float64),
                np.arange(width, dtype=np.float64),
                indexing="ij",
            )

            ring_coordinates = []
            for col_offset, row_offset in (
                (0, 0),
                (1, 0),
                (1, 1),
                (0, 1),
                (0, 0),
            ):
                pixel_cols = cols + col_offset
                pixel_rows = rows + row_offset
                x = (
                    window_transform.a * pixel_cols
                    + window_transform.b * pixel_rows
                    + window_transform.c
                )
                y = (
                    window_transform.d * pixel_cols
                    + window_transform.e * pixel_rows
                    + window_transform.f
                )
                ring_coordinates.append(np.stack((x, y), axis=-1))

            pixel_coordinates = np.stack(ring_coordinates, axis=2).reshape(
                -1, 5, 2
            )
            pixel_polygons = polygons(pixel_coordinates)
            pixel_polygons_area_crs = transform(
                pixel_polygons,
                area_transformer.transform,
                interleaved=False,
            )
            pixel_overlaps = intersection(
                pixel_polygons_area_crs,
                aoi_area_geometry,
            )
            pixel_areas = area(pixel_overlaps).reshape(row_end - row_start, width)
            valid_area += float(
                pixel_areas[valid_data[row_start:row_end]].sum()
            )

    aoi_data_coverage_percent = np.clip(
        valid_area / aoi_area * 100,
        0.0,
        float(aoi_coverage_percent),
    )
    return (
        float(aoi_coverage_percent),
        float(aoi_data_coverage_percent),
    )


def calculate_scl_quality(
    scl_path: Path,
    aoi_path: Path | None = None,
    coverage_raster_path: Path | None = None,
) -> dict:
    """Calculate SCL quality and, when supplied, AOI surface coverage.

    ``scl_path`` remains the AOI-clipped SCL used for the existing pixel-count
    quality metrics. ``coverage_raster_path`` can point to the original scene
    raster so spatial/data coverage is measured against the full acquisition
    footprint. Counts and class proportions exclude class 0 and every masked
    pixel; ``total_pixels`` is the count of observed, nonzero SCL classes.
    ``land_pixels`` excludes water, while usable land remains classes 4 and 5.
    Without ``aoi_path``, coverage indicators are ``None`` because the AOI
    denominator is unknown.
    """

    with safe_call(rasterio.open, scl_path, paths=(scl_path,)) as src:
        scl_data = src.read(1, masked=True)

    scl = np.ma.getdata(scl_data)
    dataset_mask = np.ma.getmaskarray(scl_data)

    # Exclude class 0 and every dataset-masked pixel from all counts and
    # denominators. In particular, masked cells can contain nonzero classes.
    valid_data_mask = (
        ~dataset_mask
        & ~np.isin(scl, tuple(SCL_NO_DATA_CLASSES))
    )
    total_pixels = int(np.count_nonzero(valid_data_mask))

    water_mask = (
        np.isin(scl, tuple(SCL_WATER_CLASSES)) & valid_data_mask
    )

    reliable_land_mask = (
        np.isin(scl, tuple(SCL_USABLE_LAND_CLASSES)) & valid_data_mask
    )

    uncertain_mask = (
        np.isin(scl, tuple(SCL_UNCERTAIN_CLASSES)) & valid_data_mask
    )

    cloud_shadow_mask = (
        np.isin(scl, tuple(SCL_CLOUD_SHADOW_CLASSES)) & valid_data_mask
    )

    other_invalid_mask = np.isin(
        scl,
        tuple(SCL_DEFECTIVE_CLASSES | SCL_SNOW_ICE_CLASSES),
    ) & valid_data_mask

    # Preserve the existing terrestrial denominator: every observed SCL
    # class except water, while the usable score remains classes 4 and 5.
    land_mask = valid_data_mask & ~water_mask

    land_pixels = np.count_nonzero(
        land_mask
    )

    water_pixels = np.count_nonzero(
        water_mask
    )

    reliable_land_pixels = np.count_nonzero(
        reliable_land_mask
    )

    uncertain_pixels = np.count_nonzero(
        uncertain_mask
    )

    cloud_shadow_pixels = np.count_nonzero(
        cloud_shadow_mask
    )

    if land_pixels > 0:
        usable_land_percent = (
            reliable_land_pixels
            / land_pixels
            * 100
        )

        uncertain_percent = (
            uncertain_pixels
            / land_pixels
            * 100
        )

        cloud_shadow_percent = (
            cloud_shadow_pixels
            / land_pixels
            * 100
        )
    else:
        usable_land_percent = 0.0
        uncertain_percent = 0.0
        cloud_shadow_percent = 0.0

    water_percent = water_pixels / total_pixels * 100 if total_pixels else 0.0
    invalid_percent = (
        np.count_nonzero(cloud_shadow_mask | other_invalid_mask)
        / total_pixels
        * 100
        if total_pixels
        else 0.0
    )

    if aoi_path is None:
        aoi_coverage_percent = None
        aoi_data_coverage_percent = None
    else:
        aoi_coverage_percent, aoi_data_coverage_percent = (
            _calculate_aoi_surface_coverage(
                coverage_raster_path or scl_path,
                aoi_path,
            )
        )

    return {
        "total_pixels": total_pixels,
        "land_pixels": land_pixels,
        "aoi_coverage_percent": aoi_coverage_percent,
        "aoi_data_coverage_percent": aoi_data_coverage_percent,

        "water_percent": water_percent,
        "invalid_percent": invalid_percent,

        "usable_land_pixels": reliable_land_pixels,
        "usable_land_percent": usable_land_percent,

        "uncertain_percent": uncertain_percent,
        "cloud_shadow_percent": cloud_shadow_percent,
    }

def select_best_product_for_aoi(
    items: list[dict],
    aoi_path: Path,
    s3_client,
    raw_dir: Path,
    interim_dir: Path,
) -> dict:
    """Select the Sentinel-2 product with the best usable land coverage."""

    if not items:
        raise ValueError(
            "Aucun produit Sentinel-2 à évaluer."
        )

    results = []
    failures = []
    failure_causes = []

    for item in items:
        product_id = item["id"]

        cloud_cover = item.get(
            "properties",
            {},
        ).get(
            "eo:cloud_cover"
        )

        print(f"\nAnalyse : {product_id}")
        print(f"Nuages STAC : {cloud_cover}")

        try:
            # Récupération de l'asset SCL
            scl_asset = get_asset(
                item,
                "SCL_20m",
            )

            product_raw_dir = (
                raw_dir
                / product_id
            )

            scl_path = download_asset(
                s3_client=s3_client,
                asset=scl_asset,
                destination_dir=product_raw_dir,
            )

            # Découpage du SCL sur l'AOI
            product_interim_dir = (
                interim_dir
                / product_id
            )

            clipped_scl_path = (
                product_interim_dir
                / "SCL_20m_AOI.tif"
            )

            clip_raster_to_aoi(
                input_path=scl_path,
                output_path=clipped_scl_path,
                aoi_path=aoi_path,
            )

            # Calcul de la qualité locale
            quality = calculate_scl_quality(
                clipped_scl_path,
                aoi_path=aoi_path,
                coverage_raster_path=scl_path,
            )

            result = {
                "product": item,
                "product_id": product_id,
                "cloud_cover": cloud_cover,
                **quality,
            }

            results.append(result)

            print(
                "Terrain exploitable : "
                f"{quality['usable_land_percent']:.2f} %"
            )

        except Exception as error:
            safe_message = safe_exception_message(
                error,
                paths=(aoi_path, raw_dir, interim_dir),
            )
            failure = {
                "product_id": product_id,
                "error_type": type(error).__name__,
                "message": safe_message,
            }
            failures.append(failure)
            failure_causes.append(error)
            logger.error(
                "Échec du traitement Sentinel-2 pour le produit %s (%s) : %s",
                product_id,
                type(error).__name__,
                safe_message,
                extra={"product_id": product_id, "error_type": type(error).__name__},
            )
            print(
                f"Échec du candidat "
                f"{product_id} ({type(error).__name__}) : {safe_message}"
            )

    if not results:
        failure_summary = "; ".join(
            f"{failure['product_id']} ({failure['error_type']}): "
            f"{failure['message']}"
            for failure in failures
        )
        error = RuntimeError(
            "Aucun candidat Sentinel-2 n'a pu être évalué. "
            f"Échecs : {failure_summary}"
        )
        # Preserve useful cause/type detail while ensuring chained tracebacks
        # cannot reintroduce a path removed from the public failure summary.
        original_cause = failure_causes[0]
        safe_cause_message = safe_exception_message(
            original_cause,
            paths=(aoi_path, raw_dir, interim_dir),
        )
        if safe_cause_message == str(original_cause):
            cause = original_cause
        else:
            try:
                cause = type(original_cause)(safe_cause_message)
            except Exception:
                cause = RuntimeError(
                    f"{type(original_cause).__name__}: {safe_cause_message}"
                )
        raise error from cause

    results.sort(
        key=lambda result: result[
            "usable_land_percent"
        ],
        reverse=True,
    )

    return {
        "best_product": results[0]["product"],
        "best_product_id": results[0]["product_id"],
        "best_quality": results[0],
        "ranking": results,
        "failures": failures,
    }
