"""Radar layer of a stored Sentinel-1 mosaic window; no UI, no network.

The radar stays on its native 20 m grid (no resampling of backscatter). Land
cover is aggregated onto that grid by majority class when per-class
statistics are requested. Radar values are never compared with NDVI: they
measure surface roughness/structure and moisture, not chlorophyll activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

from src.data.external_sources import ExternalLayerDescriptor
from src.data.sentinel1 import RADIOMETRY, SCALE_CONVENTIONS
from src.geo.change_analysis import _rasterize_aoi_pixel_mask
from src.geo.temporal import RasterGrid
from src.geo.zonal import ZoneStatistics, align_categorical_to_grid, zonal_statistics

# Documented by the producer (see RADIOMETRY); not inferred from value plausibility.
DEFAULT_SCALE = RADIOMETRY["convention"]
DISPLAY_RANGE_DB = {"VV": (-25.0, 0.0), "VH": (-32.0, -7.0)}


@dataclass(frozen=True)
class RadarLayer:
    polarization: str
    descriptor: ExternalLayerDescriptor
    grid: RasterGrid
    db: np.ndarray            # float32 dB, NaN where invalid.
    valid: np.ndarray         # bool: inside AOI, finite, > 0, not NoData.
    aoi_pixels: np.ndarray
    scale: str
    invalid_counts: dict
    median_db: float | None
    p25_db: float | None
    p75_db: float | None
    coverage_percent: float   # Valid cells / AOI cells (cell count, not exact area).
    per_class: tuple[ZoneStatistics, ...] = ()
    tiles: tuple = ()                 # TileContribution per window (multi-tile only).
    mosaic_notes: tuple[str, ...] = ()

    @property
    def documented(self) -> bool:
        """True only when the producer-documented convention is applied."""
        return self.scale == RADIOMETRY["convention"]

    @property
    def unit(self) -> str:
        if self.documented:
            return "dB de γ⁰ aplani du relief (10·log10 de la puissance linéaire)"
        return f"dB expérimentaux (convention « {self.scale} » non documentée par le producteur)"


def _classify(values, nodata):
    """Per-pixel code: 1 valid, 2 NoData, 3 non-finite, 4 <= 0 (log undefined)."""
    codes = np.ones(values.shape, dtype=np.uint8)
    finite = np.isfinite(values)
    codes[~finite] = 3
    if nodata is not None:
        codes[values == nodata] = 2
    codes[finite & (values <= 0) & (codes == 1)] = 4
    return codes


def _layer(polarization, descriptor, grid, values, codes, aoi_path, scale, landcover, landcover_classes,
           tiles=(), notes=()):
    aoi_pixels = _rasterize_aoi_pixel_mask(grid, aoi_path)
    valid = aoi_pixels & (codes == 1)
    db = np.full(values.shape, np.nan, dtype=np.float32)
    db[valid] = SCALE_CONVENTIONS[scale] * np.log10(values[valid])
    selected = db[valid].astype(np.float64)
    p25, median, p75 = (np.percentile(selected, [25, 50, 75]) if selected.size else (None, None, None))
    per_class = ()
    if landcover and landcover_classes:
        from src.services.multisource import categorical_tiles_on_grid

        mosaic = categorical_tiles_on_grid([(Path(path).stem, path) for path in landcover], grid,
                                           resampling=Resampling.mode)
        per_class = zonal_statistics(db, valid & mosaic.valid, mosaic.values, landcover_classes)
    inside = int(np.count_nonzero(aoi_pixels))
    counts = {"nodata": int(np.count_nonzero((codes == 2) & aoi_pixels)),
              "non_fini": int(np.count_nonzero((codes == 3) & aoi_pixels)),
              "non_positif": int(np.count_nonzero((codes == 4) & aoi_pixels))}
    if tiles:
        counts["hors_emprise"] = int(np.count_nonzero((codes == 0) & aoi_pixels))
    return RadarLayer(
        polarization=polarization, descriptor=descriptor, grid=grid, db=db, valid=valid, aoi_pixels=aoi_pixels,
        scale=scale, invalid_counts=counts,
        median_db=None if median is None else float(median), p25_db=None if p25 is None else float(p25),
        p75_db=None if p75 is None else float(p75),
        coverage_percent=100.0 * np.count_nonzero(valid) / inside if inside else 0.0, per_class=per_class,
        tiles=tuple(tiles), mosaic_notes=tuple(notes))


def _check(descriptor, scale):
    if scale not in SCALE_CONVENTIONS:
        raise ValueError("Convention d'échelle radar inconnue.")
    polarization = descriptor.provenance.get("polarization")
    if descriptor.source_id != "sentinel-1-mosaic" or polarization not in ("VV", "VH"):
        raise ValueError("Descripteur Sentinel-1 VV/VH requis.")
    return polarization


def _landcover_paths(landcover_path):
    if landcover_path is None:
        return []
    return [landcover_path] if isinstance(landcover_path, (str, Path)) else list(landcover_path)


def load_radar_layer(path: str | Path, descriptor: ExternalLayerDescriptor, *, aoi_path: str | Path,
                     scale: str = DEFAULT_SCALE, landcover_path=None,
                     landcover_classes: dict | None = None) -> RadarLayer:
    """One stored window on its native grid. ``landcover_path`` may be one path or several tiles."""
    polarization = _check(descriptor, scale)
    with rasterio.open(path) as source:
        values = source.read(1).astype(np.float32)
        codes = _classify(values, source.nodata)
        grid = RasterGrid(source.crs, (abs(source.transform.a), abs(source.transform.e)), source.transform,
                          source.width, source.height)
    return _layer(polarization, descriptor, grid, values, codes, aoi_path, scale,
                  _landcover_paths(landcover_path), landcover_classes)


def load_radar_mosaic(layers, *, aoi_path: str | Path, scale: str = DEFAULT_SCALE, landcover_path=None,
                      landcover_classes: dict | None = None) -> RadarLayer:
    """Several stored windows [(path, descriptor)] of one polarization and one month.

    Target: native 20 m grid (CRS and lattice) of the first window by item id,
    covering the AOI. Other windows are aligned by nearest neighbour (no
    interpolation of backscatter); overlaps follow ``src.geo.mosaic``.
    """
    import geopandas as gpd
    from rasterio.warp import reproject
    from src.geo.mosaic import AlignedTile, aoi_grid, mosaic_tiles, same_lattice

    layers = sorted(layers, key=lambda item: item[1].version)
    if len(layers) == 1:
        return load_radar_layer(*layers[0], aoi_path=aoi_path, scale=scale, landcover_path=landcover_path,
                                landcover_classes=landcover_classes)
    polarizations = {_check(descriptor, scale) for _, descriptor in layers}
    months = {descriptor.temporal.start_utc for _, descriptor in layers}
    if len(polarizations) != 1 or len(months) != 1:
        raise ValueError("Fenêtres radar de polarisations ou de mois différents : mosaïque refusée.")
    aoi = gpd.read_file(aoi_path)
    with rasterio.open(layers[0][0]) as first:
        grid = aoi_grid(tuple(aoi.total_bounds), aoi.crs, first.crs,
                        (abs(first.transform.a), abs(first.transform.e)), (first.transform.c, first.transform.f))
    aligned, reasons = [], []
    for path, descriptor in layers:
        with rasterio.open(path) as source:
            values = source.read(1).astype(np.float32)
            codes = _classify(values, source.nodata)
            target_values = np.zeros((grid.height, grid.width), np.float32)
            target_codes = np.zeros((grid.height, grid.width), np.uint8)
            for array, destination in ((np.where(codes == 1, values, 0).astype(np.float32), target_values),
                                       (codes, target_codes)):
                reproject(source=array, destination=destination, src_transform=source.transform,
                          src_crs=source.crs, dst_transform=grid.transform, dst_crs=grid.crs,
                          resampling=Resampling.nearest, init_dest_nodata=False, src_nodata=None, dst_nodata=None)
            aligned.append(AlignedTile(descriptor.version, source.crs.to_string(), tuple(source.res),
                                       same_lattice(source.transform, grid, source.crs), target_values,
                                       target_codes == 1))
            reasons.append(target_codes)
    mosaic = mosaic_tiles(aligned, categorical=False, tolerance=1e-6)
    codes = np.where(mosaic.valid, 1, 0).astype(np.uint8)
    for reason in reasons:  # First tile reporting an invalid reason explains an empty cell.
        codes = np.where((codes == 0) & (reason > 1), reason, codes)
    note = f"{len(layers)} tuiles MGRS ; recouvrement {mosaic.overlap_cells} cellules"
    if mosaic.max_abs_difference is not None:
        note += f", écart max. {mosaic.max_abs_difference:.2e}"
    values = np.where(mosaic.valid, mosaic.values, 0).astype(np.float32)
    return _layer(polarizations.pop(), layers[0][1], grid, values, codes, aoi_path, scale,
                  _landcover_paths(landcover_path), landcover_classes, mosaic.contributions, (note,))
