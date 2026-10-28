"""The study zone seen by indicator calculations: a polygonal geometry file, whatever its origin.

Calculations receive a path and read the geometry here only. Today the files
are GEONAF acquisition squares (``data/aoi``); registry territories and user
imports are already stored as GeoJSON files (``data/territories/geometries``),
so they can enter through the same contract. Identity is the file digest:
results and caches are never shared between two geometries.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def zone_sha256(aoi_path) -> str:
    return hashlib.sha256(Path(aoi_path).read_bytes()).hexdigest()


def metric_zone(aoi_path):
    """AOI union in its local UTM zone (lengths, footprints); the zone follows the AOI anywhere in Africa."""
    import geopandas as gpd

    aoi = gpd.read_file(aoi_path)
    if aoi.empty or aoi.crs is None:
        raise ValueError("L'AOI doit être non vide et avoir un CRS.")
    crs = aoi.estimate_utm_crs()
    return aoi.to_crs(crs).geometry.union_all(), crs


def zone_area_km2(aoi_path) -> float:
    """Geodesic area of the zone (equal-area EPSG:6933, as for raster weights)."""
    from src.geo.change_analysis import load_equal_area_aoi

    return load_equal_area_aoi(aoi_path).area_m2 / 1e6
