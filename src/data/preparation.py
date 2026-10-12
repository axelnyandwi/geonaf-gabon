"""Préparation et harmonisation des données à développer."""
"""Preparation and transformation of geospatial raster data."""

from pathlib import Path
from rasterio.windows import from_bounds
from rasterio.warp import transform_bounds

import rasterio
import numpy as np
from src.diagnostics import safe_call

def inspect_raster(raster_path: Path) -> dict:
    """Read the main metadata of a raster file."""

    if not raster_path.exists():
        raise FileNotFoundError("Raster introuvable.")

    with safe_call(rasterio.open, raster_path, paths=(raster_path,)) as dataset:
        return {
            "path": raster_path,
            "width": dataset.width,
            "height": dataset.height,
            "bands": dataset.count,
            "dtype": dataset.dtypes[0],
            "crs": dataset.crs.to_string() if dataset.crs else None,
            "resolution": dataset.res,
            "bounds": dataset.bounds,
            "nodata": dataset.nodata,
        }

def clip_raster_to_bbox(
    input_path: Path,
    output_path: Path,
    bbox: tuple[float, float, float, float],
) -> Path:
    """Clip a raster using a bounding box expressed in the raster CRS."""

    safe_call(
        output_path.parent.mkdir,
        parents=True,
        exist_ok=True,
        paths=(output_path.parent,),
    )

    with safe_call(rasterio.open, input_path, paths=(input_path,)) as src:
        window = from_bounds(
            *bbox,
            transform=src.transform,
        )

        data = src.read(window=window)

        transform = src.window_transform(window)

        profile = src.profile.copy()
        profile.update(
            width=data.shape[2],
            height=data.shape[1],
            transform=transform,
        )

        with safe_call(rasterio.open, output_path, "w", paths=(output_path,), **profile) as dst:
            dst.write(data)

    return output_path

def transform_bbox(
    bbox: tuple[float, float, float, float],
    source_crs: str,
    target_crs: str,
) -> tuple[float, float, float, float]:
    """Transform a bounding box between coordinate reference systems."""

    return transform_bounds(
        source_crs,
        target_crs,
        *bbox,
    )

def clip_raster(
    input_path: Path,
    output_path: Path,
    bbox: tuple[float, float, float, float],
    bbox_crs: str = "EPSG:4326",
) -> Path:
    """Clip a raster from a bbox and automatically handle CRS conversion."""

    with safe_call(rasterio.open, input_path, paths=(input_path,)) as src:
        if src.crs is None:
            raise ValueError("Le raster ne possède pas de CRS.")

        raster_crs = src.crs.to_string()

    raster_bbox = transform_bbox(
        bbox=bbox,
        source_crs=bbox_crs,
        target_crs=raster_crs,
    )

    return clip_raster_to_bbox(
        input_path=input_path,
        output_path=output_path,
        bbox=raster_bbox,
    )

def clip_raster_to_aoi(
    input_path: Path,
    output_path: Path,
    aoi_path: Path,
) -> Path:
    """Clip a raster using an AOI GeoJSON."""

    import geopandas as gpd
    from rasterio.mask import mask

    # 1. Lecture de l'AOI
    aoi = safe_call(gpd.read_file, aoi_path, paths=(aoi_path,))

    if aoi.empty:
        raise ValueError("AOI vide.")

    if aoi.crs is None:
        raise ValueError("L'AOI ne possède pas de CRS.")

    # 2. Ouverture du raster
    with safe_call(rasterio.open, input_path, paths=(input_path,)) as src:

        if src.crs is None:
            raise ValueError("Le raster ne possède pas de CRS.")

        # 3. Reprojection automatique de l'AOI
        aoi_raster_crs = aoi.to_crs(src.crs)

        geometries = list(aoi_raster_crs.geometry)

        # 4. Découpage
        clipped_data, clipped_transform = mask(
            src,
            geometries,
            crop=True,
        )

        # 5. Copie des métadonnées du raster
        profile = src.profile.copy()

        profile.update(
            {
                "height": clipped_data.shape[1],
                "width": clipped_data.shape[2],
                "transform": clipped_transform,
            }
        )

    # 6. Création du dossier de sortie
    safe_call(
        output_path.parent.mkdir,
        parents=True,
        exist_ok=True,
        paths=(output_path.parent,),
    )

    # 7. Écriture du raster découpé
    with safe_call(
        rasterio.open,
        output_path,
        "w",
        paths=(output_path,),
        **profile,
    ) as dst:
        dst.write(clipped_data)

    return output_path

def clip_rgb_bands(
    product_dir: Path,
    output_dir: Path,
    bbox: tuple[float, float, float, float],
) -> dict[str, Path]:
    """Clip the Sentinel-2 RGB bands to the same area."""

    band_patterns = {
        "B02": "*_B02_10m.jp2",
        "B03": "*_B03_10m.jp2",
        "B04": "*_B04_10m.jp2",
    }

    outputs = {}

    for band_name, pattern in band_patterns.items():
        matches = list(product_dir.glob(pattern))

        if len(matches) != 1:
            raise ValueError(
                f"Attendu 1 fichier pour {band_name}, "
                f"trouvé : {len(matches)}"
            )

        output_path = output_dir / f"{band_name}_10m.tif"

        outputs[band_name] = clip_raster(
            input_path=matches[0],
            output_path=output_path,
            bbox=bbox,
        )

    return outputs

def normalize_band(
    band: np.ndarray,
    lower_percentile: float = 2,
    upper_percentile: float = 98,
) -> np.ndarray:
    """Normalize a satellite band for visualization."""

    valid_pixels = band[band > 0]

    if valid_pixels.size == 0:
        return np.zeros_like(band, dtype=np.float32)

    low, high = np.percentile(
        valid_pixels,
        [lower_percentile, upper_percentile],
    )

    if high <= low:
        return np.zeros_like(band, dtype=np.float32)

    normalized = (band.astype(np.float32) - low) / (high - low)

    return np.clip(normalized, 0, 1)

def create_rgb_array(
    red_path: Path,
    green_path: Path,
    blue_path: Path,
) -> np.ndarray:
    """Create a natural-color RGB array from Sentinel-2 bands."""

    with safe_call(rasterio.open, red_path, paths=(red_path,)) as src:
        red = src.read(1)

    with safe_call(rasterio.open, green_path, paths=(green_path,)) as src:
        green = src.read(1)

    with safe_call(rasterio.open, blue_path, paths=(blue_path,)) as src:
        blue = src.read(1)

    if red.shape != green.shape or red.shape != blue.shape:
        raise ValueError(
            "Les bandes RGB n'ont pas les mêmes dimensions."
        )

    red = normalize_band(red)
    green = normalize_band(green)
    blue = normalize_band(blue)

    return np.dstack((red, green, blue))

def calculate_ndvi(
    red_path: Path,
    nir_path: Path,
    output_path: Path,
    *,
    calibration_xml: Path | None = None,
) -> Path:
    """Write NDVI through the canonical implementation.

    Supplying ``calibration_xml`` applies the product's BOA offsets and
    quantification value. Without it, zero offsets and unit scale preserve the
    historical raw-ratio behavior and avoid applying offsets to reflectance
    rasters that are already calibrated. Raw Sentinel-2 DN inputs should pass
    their product metadata explicitly.
    """

    import numpy as np
    from src.geo.vegetation_indices import calculate_ndvi as calculate_calibrated_ndvi

    if calibration_xml is None:
        red_offset = 0.0
        nir_offset = 0.0
        quantification_value = 1.0
    else:
        from src.data.radiometric_calibration import read_sentinel2_calibration

        calibration = read_sentinel2_calibration(calibration_xml)
        red_offset = calibration["offsets"]["B04"]
        nir_offset = calibration["offsets"]["B08"]
        quantification_value = calibration["quantification_value"]

    ndvi, profile = calculate_calibrated_ndvi(
        red_path=red_path,
        nir_path=nir_path,
        valid_mask=None,
        red_offset=red_offset,
        nir_offset=nir_offset,
        quantification_value=quantification_value,
    )

    profile.update(
        driver="GTiff",
        dtype="float32",
        count=1,
        nodata=np.nan,
        compress="deflate",
    )

    safe_call(
        output_path.parent.mkdir,
        parents=True,
        exist_ok=True,
        paths=(output_path.parent,),
    )

    with safe_call(
        rasterio.open,
        output_path,
        "w",
        paths=(output_path,),
        **profile,
    ) as dst:
        dst.write(ndvi, 1)

    return output_path

def mask_ndvi_with_scl(
    ndvi_path: Path,
    scl_path: Path,
    output_path: Path,
    policy: str = "land_surface",
) -> Path:
    """Mask NDVI using an explicit SCL policy on the NDVI raster grid.

    The default ``land_surface`` retains classes 2, 4, and 5. Class 7 remains
    an observed SCL classification, but is excluded from land-surface analysis
    because it is unclassified.
    """

    import numpy as np
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
    from src.data.cloud_mask import scl_mask_from_values

    with safe_call(rasterio.open, ndvi_path, paths=(ndvi_path,)) as ndvi_src:
        ndvi = ndvi_src.read(1, masked=True).astype("float32")
        profile = ndvi_src.profile.copy()
        if ndvi_src.crs is None:
            raise ValueError("CRS du NDVI manquant.")

    with safe_call(rasterio.open, scl_path, paths=(scl_path,)) as scl_src:
        if scl_src.crs is None:
            raise ValueError("CRS du raster SCL manquant.")
        source_scl = scl_src.read(1, masked=True)
        source_validity = ~np.ma.getmaskarray(source_scl)
        destination_shape = (profile["height"], profile["width"])
        aligned_values = np.zeros(destination_shape, dtype=np.uint8)
        aligned_validity = np.zeros(destination_shape, dtype=np.uint8)
        reproject(
            source=np.ma.getdata(source_scl).astype(np.uint8),
            destination=aligned_values,
            src_transform=scl_src.transform,
            src_crs=scl_src.crs,
            dst_transform=profile["transform"],
            dst_crs=profile["crs"],
            resampling=Resampling.nearest,
        )
        reproject(
            source=source_validity.astype(np.uint8),
            destination=aligned_validity,
            src_transform=scl_src.transform,
            src_crs=scl_src.crs,
            dst_transform=profile["transform"],
            dst_crs=profile["crs"],
            resampling=Resampling.nearest,
        )

    valid_scl = scl_mask_from_values(
        aligned_values,
        policy=policy,
        valid_mask=aligned_validity.astype(bool),
    )
    ndvi_values = np.ma.getdata(ndvi)
    invalid_ndvi = np.ma.getmaskarray(ndvi) | ~np.isfinite(ndvi_values)
    ndvi_clean = np.asarray(ndvi_values, dtype=np.float32).copy()
    ndvi_clean[~valid_scl | invalid_ndvi] = np.nan

    profile.update(
        driver="GTiff",
        dtype="float32",
        nodata=np.nan,
        compress="deflate",
    )

    safe_call(
        output_path.parent.mkdir,
        parents=True,
        exist_ok=True,
        paths=(output_path.parent,),
    )

    with safe_call(
        rasterio.open,
        output_path,
        "w",
        paths=(output_path,),
        **profile,
    ) as dst:
        dst.write(ndvi_clean, 1)

    return output_path
