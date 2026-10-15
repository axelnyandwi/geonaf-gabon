"""GeoTIFF and JSON serialization for temporal NDVI change results."""

from __future__ import annotations

import json
import math
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Mapping

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio.features import rasterize
from shapely import is_empty, is_valid
from shapely.geometry import mapping

from src.diagnostics import safe_call
from src.geo.change_analysis import ChangeClass
from src.services.temporal_analysis import TemporalVegetationChangeResult


_SCHEMA_VERSION = "1.0"
_TAG_VALUE_LIMIT = 256


def _json_value(value):
    """Convert traceability metadata to JSON without serializing raster arrays."""
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Affine):
        return list(value)[:6]
    if isinstance(value, (Path, rasterio.crs.CRS)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Type de métadonnée non pris en charge pour JSON : {type(value).__name__}.")


def _aoi_pixel_mask(result: TemporalVegetationChangeResult) -> np.ndarray:
    grid = result.common_grid
    if result.aoi_path is None:
        return np.ones((grid.height, grid.width), dtype=bool)

    aoi = safe_call(gpd.read_file, result.aoi_path, paths=(result.aoi_path,))
    if aoi.empty or aoi.crs is None:
        raise ValueError("L'AOI exportée doit être non vide et avoir un CRS.")
    if any(geometry is None or is_empty(geometry) or not is_valid(geometry) for geometry in aoi.geometry):
        raise ValueError("L'AOI exportée contient une géométrie vide ou invalide.")
    geometries = aoi.to_crs(grid.crs).geometry
    shapes = [(mapping(geometry), 1) for geometry in geometries]
    return rasterize(
        shapes,
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        fill=0,
        all_touched=True,
        dtype="uint8",
    ).astype(bool)


def _season_metadata(result: TemporalVegetationChangeResult) -> dict:
    assessment = result.seasonal_assessment
    return {
        "relation": assessment.season_relation,
        "assessment": _json_value(assessment),
        "earlier_context": _json_value(result.earlier.seasonal_context),
        "later_context": _json_value(result.later.seasonal_context),
    }


def _manifest(result, exported_pixel_counts: dict[str, int], valid_pixel_count: int) -> dict:
    analysis = result.analysis
    return {
        "schema_version": _SCHEMA_VERSION,
        "product": "GEONAF temporal Sentinel-2 NDVI variation",
        "interpretation_scope": result.interpretation_scope,
        "inputs": {
            "earlier": _json_value(result.earlier),
            "later": _json_value(result.later),
            "selected_pair": _json_value(result.selected_pair),
        },
        "seasonality": _season_metadata(result),
        "processing": {
            "scl_policy": result.scl_policy,
            "change_threshold": result.threshold,
            "delta_definition": "NDVI date 2 minus NDVI date 1",
            "change_class_codes": {
                "non_comparable_nodata": int(ChangeClass.NON_COMPARABLE),
                "decrease": int(ChangeClass.DECREASE),
                "stable": int(ChangeClass.STABLE),
                "increase": int(ChangeClass.INCREASE),
            },
            "aoi_path": result.aoi_path,
            "grid": {
                "crs": str(result.common_grid.crs),
                "resolution": list(result.common_grid.resolution),
                "transform": list(result.common_grid.transform)[:6],
                "width": result.common_grid.width,
                "height": result.common_grid.height,
            },
        },
        "analysis_indicators": {
            "statistics": _json_value(analysis.statistics),
            "areas_m2": _json_value(analysis.areas_m2),
            "areas_ha": _json_value(analysis.areas_ha),
            "class_pixel_counts": _json_value(analysis.class_pixel_counts),
            "comparable_area_m2": analysis.comparable_area_m2,
            "denominator_area_m2": analysis.denominator_area_m2,
            "comparable_coverage_percent": analysis.comparable_coverage_percent,
            "coverage_denominator": analysis.coverage_denominator,
            "excluded_out_of_range_pixels": analysis.excluded_out_of_range_pixels,
        },
        "raster_representation": {
            "aoi_pixelization": "all_touched: chaque cellule intersectant l'AOI est incluse",
            "valid_pixel_count": valid_pixel_count,
            "exported_pixel_counts": exported_pixel_counts,
            "nodata": {"change_class": 0, "delta_ndvi": "NaN"},
            "surface_note": (
                "Les surfaces et indicateurs sont repris du calcul métier et pondérés par "
                "l'intersection exacte avec l'AOI. Le nombre de pixels rasterisés peut donc "
                "différer d'une surface exprimée en unités de pixels complets."
            ),
        },
    }


def _tag_text(value) -> str:
    text = str(value)
    if len(text) > _TAG_VALUE_LIMIT:
        return text[: _TAG_VALUE_LIMIT - 3] + "..."
    return text


def _write_raster(path: Path, result, values: np.ndarray, valid_mask: np.ndarray,
                  *, dtype: str, nodata, description: str, tags: dict) -> None:
    grid = result.common_grid
    profile = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": 1,
        "dtype": dtype,
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": nodata,
        "compress": "deflate",
    }
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        with rasterio.open(path, "w", **profile) as destination:
            destination.write(values, 1)
            destination.write_mask(valid_mask.astype(np.uint8) * 255)
            destination.set_band_description(1, description)
            destination.update_tags(AREA_OR_POINT="Area", **tags)


def export_temporal_change_geotiff(
    result: TemporalVegetationChangeResult,
    class_raster_path: str | Path,
) -> dict[str, Path]:
    """Write class and delta rasters with bounded tags and full JSON provenance.

    Existing outputs are never overwritten. AOI edge cells use ``all_touched``
    for raster representation; area indicators remain those calculated by the
    scientific analysis using exact AOI/pixel intersection weights.
    """
    class_path = Path(class_raster_path)
    if class_path.suffix.casefold() not in {".tif", ".tiff"}:
        raise ValueError("Le chemin du raster de classes doit finir par .tif ou .tiff.")
    delta_path = class_path.with_name(f"{class_path.stem}_delta.tif")
    manifest_path = class_path.with_name(f"{class_path.stem}_manifest.json")
    output_paths = (class_path, delta_path, manifest_path)
    if len({path.resolve() for path in output_paths}) != len(output_paths):
        raise ValueError("Les chemins de sortie doivent être distincts.")
    existing = next((path for path in output_paths if path.exists()), None)
    if existing is not None:
        raise FileExistsError(f"Une sortie existe déjà : {existing.name}")

    analysis = result.analysis
    expected_shape = (result.common_grid.height, result.common_grid.width)
    classes = np.asarray(analysis.change_class_map)
    delta = np.asarray(analysis.delta_ndvi, dtype=np.float32)
    comparable = np.asarray(analysis.comparable_mask, dtype=bool)
    if any(array.shape != expected_shape for array in (classes, delta, comparable)):
        raise ValueError("Les tableaux d'analyse ne correspondent pas à la grille commune.")
    aoi_mask = _aoi_pixel_mask(result)
    valid = comparable & aoi_mask
    if np.any(valid & ~np.isfinite(delta)):
        raise ValueError("Un pixel déclaré comparable ne possède pas de delta NDVI fini.")

    class_values = np.asarray(classes, dtype=np.uint8).copy()
    delta_values = delta.copy()
    class_values[~valid] = ChangeClass.NON_COMPARABLE
    delta_values[~valid] = np.nan
    exported_counts = {
        name: int(np.count_nonzero(valid & (class_values == class_value)))
        for name, class_value in (
            ("decrease", ChangeClass.DECREASE),
            ("stable", ChangeClass.STABLE),
            ("increase", ChangeClass.INCREASE),
        )
    }
    manifest = _manifest(result, exported_counts, int(np.count_nonzero(valid)))
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    short_tags = {
        "GEONAF_SCHEMA": _SCHEMA_VERSION,
        "PRODUCT_EARLIER": _tag_text(result.earlier.product_id),
        "PRODUCT_LATER": _tag_text(result.later.product_id),
        "ACQUIRED_EARLIER_UTC": _tag_text(result.earlier.acquired_at_utc.isoformat()),
        "ACQUIRED_LATER_UTC": _tag_text(result.later.acquired_at_utc.isoformat()),
        "SEASON_RELATION": _tag_text(result.seasonal_assessment.season_relation),
        "SEASON_STATUS": _tag_text(result.seasonal_assessment.status),
        "SEASON_WARNING_COUNT": str(len(result.seasonal_assessment.warnings)),
        "AOI_PIXEL_RULE": "all_touched",
        "MANIFEST": _tag_text(manifest_path.name),
    }

    class_path.parent.mkdir(parents=True, exist_ok=True)
    reserved: list[Path] = []
    try:
        for path in output_paths:
            with path.open("xb"):
                pass
            reserved.append(path)
        _write_raster(
            class_path, result, class_values, valid,
            dtype="uint8", nodata=0, description="NDVI change class", tags=short_tags,
        )
        _write_raster(
            delta_path, result, delta_values, valid,
            dtype="float32", nodata=np.nan, description="NDVI date 2 minus date 1", tags=short_tags,
        )
        with manifest_path.open("w", encoding="utf-8", newline="\n") as manifest_file:
            manifest_file.write(manifest_text)
    except Exception:
        for path in reserved:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise

    return {"change_class": class_path, "delta_ndvi": delta_path, "manifest": manifest_path}
