"""NDVI of one Sentinel-2 acquisition on its native AOI grid; no UI, no network.

Same chain as the NDVI series (``prepare_date_ndvi``): AOI clip, BOA
calibration from the product XML, NDVI, profile SCL policy, values outside
[-1, 1] rejected. The grid is the clipped 10 m grid of the acquisition itself,
so external layers are aligned onto a measured grid, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from src.data.acquisition import Sentinel2Acquisition
from src.geo.change_analysis import (
    _rasterize_aoi_pixel_mask, load_equal_area_aoi, measure_mask_coverage, prepare_pixel_area_weights,
)
from src.geo.scientific_profile import ScientificProfile
from src.geo.temporal import RasterGrid, _read_ndvi_on_grid, grid_from_raster, scl_policy_mask_on_grid
from src.services.ndvi_time_series import NDVISummary, _summary, prepare_date_ndvi
from src.services.temporal_analysis import _profile_context, _revalidate, _utc


@dataclass(frozen=True)
class NDVIObservation:
    """Measured NDVI of one date; ``valid`` already excludes the exterior of the AOI."""

    product_id: str
    acquired_at_utc: datetime
    grid: RasterGrid
    ndvi: np.ndarray          # float32, NaN where invalid.
    valid: np.ndarray         # bool: inside AOI, SCL policy, raster validity, NDVI in [-1, 1].
    aoi_pixels: np.ndarray    # bool: cells touched by the AOI.
    pixel_area_weights: np.ndarray
    scl_policy: str
    valid_coverage_percent: float
    valid_area_m2: float
    summary: NDVISummary
    calibration: dict
    aoi_sha256: str
    scientific_profile: ScientificProfile
    source: str = "Copernicus Sentinel-2 L2A (B04, B08 à 10 m ; SCL à 20 m ré-échantillonné au plus proche voisin)"
    product_ids: tuple[str, ...] = ()       # All products of a multi-tile acquisition.
    tiles: tuple = ()                       # TileContribution per product (multi-tile only).
    mosaic_notes: tuple[str, ...] = ()


def analyze_ndvi_observation(acquisition: Sentinel2Acquisition, *, aoi_path: str | Path,
                             scientific_profile: ScientificProfile) -> NDVIObservation:
    if not isinstance(scientific_profile, ScientificProfile):
        raise TypeError("Un ScientificProfile est requis.")
    aoi_path = Path(aoi_path)
    acquisition = _profile_context(_revalidate(acquisition), scientific_profile)
    policy = scientific_profile.scl_policy
    with TemporaryDirectory(prefix="geonaf-ndvi-date-") as scratch:
        ndvi_path, scl_path = prepare_date_ndvi(acquisition, aoi_path, Path(scratch), "date")
        grid = grid_from_raster(ndvi_path)
        values, raster_valid = _read_ndvi_on_grid(ndvi_path, grid, scl_path, policy)
        scl_valid = scl_policy_mask_on_grid(scl_path, grid, policy)
    aoi_pixels = _rasterize_aoi_pixel_mask(grid, aoi_path)
    weights = prepare_pixel_area_weights(grid, load_equal_area_aoi(aoi_path))
    valid = raster_valid & scl_valid & np.isfinite(values) & (values >= -1.0) & (values <= 1.0) & aoi_pixels
    values = np.where(valid, values, np.nan).astype(np.float32)
    coverage = measure_mask_coverage(valid, grid, load_equal_area_aoi(aoi_path), pixel_area_weights=weights)
    calibration = acquisition.calibration
    return NDVIObservation(
        product_id=acquisition.product_id, acquired_at_utc=_utc(acquisition.acquired_at), grid=grid,
        ndvi=values, valid=valid, aoi_pixels=aoi_pixels, pixel_area_weights=weights, scl_policy=policy,
        valid_coverage_percent=coverage.coverage_percent, valid_area_m2=coverage.area_m2,
        summary=_summary(values[valid]),
        calibration={"quantification_value": calibration.quantification_value,
                     "red_offset": calibration.red_offset, "nir_offset": calibration.nir_offset},
        aoi_sha256=hashlib.sha256(aoi_path.read_bytes()).hexdigest(), scientific_profile=scientific_profile)


ACQUISITION_WINDOW = timedelta(minutes=15)


def relative_orbit(product_id: str) -> str | None:
    match = re.search(r"_R(\d{3})_", product_id)
    return match.group(1) if match else None


def mgrs_tile(product_id: str) -> str | None:
    match = re.search(r"_T(\d{2}[A-Z]{3})_", product_id)
    return match.group(1) if match else None


def validate_acquisition_group(product_ids, instants) -> None:
    """One datatake only: same relative orbit, instants within 15 min, distinct MGRS tiles."""
    if not product_ids:
        raise ValueError("Groupe d'acquisitions vide.")
    orbits = {relative_orbit(product_id) for product_id in product_ids}
    if len(orbits) != 1 or None in orbits:
        raise ValueError("Orbites relatives différentes ou inconnues : mosaïque refusée.")
    if max(instants) - min(instants) > ACQUISITION_WINDOW:
        raise ValueError("Acquisitions de dates incompatibles : mosaïque refusée.")
    tiles = [mgrs_tile(product_id) for product_id in product_ids]
    if None in tiles or len(set(tiles)) != len(tiles):
        raise ValueError("Tuiles MGRS inconnues ou en double dans le groupe.")


def analyze_ndvi_mosaic(acquisitions, *, aoi_path: str | Path, scientific_profile: ScientificProfile
                        ) -> NDVIObservation:
    """NDVI of one datatake over an AOI covered by one or several MGRS tiles.

    A single tile follows :func:`analyze_ndvi_observation` exactly. Several
    tiles are aligned on a grid covering the AOI in the CRS and 10 m lattice of
    the first intersecting product (sorted by identifier); same-zone tiles
    share that lattice and are read without resampling. Overlaps follow
    ``src.geo.mosaic`` (first valid tile wins, differences reported).
    """
    import geopandas as gpd
    import rasterio
    from src.geo.mosaic import AlignedTile, aoi_grid, mosaic_tiles, same_lattice

    acquisitions = list(acquisitions)
    if len(acquisitions) == 1:
        return analyze_ndvi_observation(acquisitions[0], aoi_path=aoi_path, scientific_profile=scientific_profile)
    if not isinstance(scientific_profile, ScientificProfile):
        raise TypeError("Un ScientificProfile est requis.")
    validate_acquisition_group([a.product_id for a in acquisitions], [_utc(a.acquired_at) for a in acquisitions])
    aoi_path = Path(aoi_path)
    policy = scientific_profile.scl_policy
    ordered = sorted((_profile_context(_revalidate(a), scientific_profile) for a in acquisitions),
                     key=lambda a: a.product_id)
    aoi = gpd.read_file(aoi_path)
    notes, aligned, used = [], [], []
    with TemporaryDirectory(prefix="geonaf-ndvi-mosaic-") as scratch:
        prepared = []
        for index, acquisition in enumerate(ordered):
            try:
                prepared.append((acquisition, *prepare_date_ndvi(acquisition, aoi_path, Path(scratch), f"t{index}")))
            except ValueError:
                notes.append(f"{acquisition.product_id} : aucune intersection avec l'AOI, tuile ignorée.")
        if not prepared:
            raise ValueError("Aucune tuile du groupe ne recoupe l'AOI.")
        with rasterio.open(prepared[0][1]) as first:
            crs, origin = first.crs, (first.transform.c, first.transform.f)
            resolution = (abs(first.transform.a), abs(first.transform.e))
        grid = aoi_grid(tuple(aoi.total_bounds), aoi.crs, crs, resolution, origin)
        for acquisition, ndvi_path, scl_path in prepared:
            with rasterio.open(ndvi_path) as source:
                native = same_lattice(source.transform, grid, source.crs)
                tile_crs, tile_res = source.crs.to_string(), (abs(source.transform.a), abs(source.transform.e))
            values, raster_valid = _read_ndvi_on_grid(ndvi_path, grid, scl_path, policy)
            valid = (raster_valid & scl_policy_mask_on_grid(scl_path, grid, policy) & np.isfinite(values)
                     & (values >= -1.0) & (values <= 1.0))
            aligned.append(AlignedTile(acquisition.product_id, tile_crs, tile_res, native, values, valid))
            used.append(acquisition)
    mosaic = mosaic_tiles(aligned, categorical=False, tolerance=1e-4)
    if mosaic.overlap_cells:
        notes.append(f"Recouvrement de {mosaic.overlap_cells} cellules entre tuiles : valeur de la tuile prioritaire "
                     f"(écart max. {mosaic.max_abs_difference:.2e}, {mosaic.conflicting_cells} cellules > 1e-4).")
    aoi_pixels = _rasterize_aoi_pixel_mask(grid, aoi_path)
    equal_area = load_equal_area_aoi(aoi_path)
    weights = prepare_pixel_area_weights(grid, equal_area)
    valid = mosaic.valid & aoi_pixels
    values = np.where(valid, mosaic.values, np.nan).astype(np.float32)
    coverage = measure_mask_coverage(valid, grid, equal_area, pixel_area_weights=weights)
    return NDVIObservation(
        product_id=" + ".join(a.product_id for a in used), acquired_at_utc=min(_utc(a.acquired_at) for a in used),
        grid=grid, ndvi=values, valid=valid, aoi_pixels=aoi_pixels, pixel_area_weights=weights, scl_policy=policy,
        valid_coverage_percent=coverage.coverage_percent, valid_area_m2=coverage.area_m2,
        summary=_summary(values[valid]),
        calibration={a.product_id: {"quantification_value": a.calibration.quantification_value,
                                    "red_offset": a.calibration.red_offset, "nir_offset": a.calibration.nir_offset}
                     for a in used},
        aoi_sha256=hashlib.sha256(aoi_path.read_bytes()).hexdigest(), scientific_profile=scientific_profile,
        product_ids=tuple(a.product_id for a in used), tiles=mosaic.contributions, mosaic_notes=tuple(notes))
