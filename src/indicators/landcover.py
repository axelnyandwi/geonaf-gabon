"""Land cover areas inside the zone, on the product's native grid with exact equal-area weights."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def _landcover_grid(tiles):
    """(grid, codes, valid) of WorldCover tiles on their own lattice; no resampling of classes."""
    import rasterio
    from rasterio.transform import from_origin

    from src.geo.temporal import RasterGrid, grid_from_raster
    from src.services.multisource import categorical_tiles_on_grid

    paths = [Path(path) for path, _ in tiles]
    if len(paths) == 1:
        grid = grid_from_raster(paths[0])
        with rasterio.open(paths[0]) as source:
            codes = source.read(1)
            valid = codes != 0
            if source.nodata is not None:
                valid &= codes != source.nodata
        return grid, codes, valid
    with rasterio.open(paths[0]) as first:
        crs, (xres, yres) = first.crs, first.res
    bounds = []
    for path in paths:
        with rasterio.open(path) as source:
            if source.crs != crs or not np.allclose(source.res, (xres, yres)):
                raise ValueError("Tuiles d'occupation du sol sur des grilles différentes.")
            bounds.append(source.bounds)
    left, top = min(b.left for b in bounds), max(b.top for b in bounds)
    width = int(round((max(b.right for b in bounds) - left) / xres))
    height = int(round((top - min(b.bottom for b in bounds)) / yres))
    grid = RasterGrid(crs, (xres, yres), from_origin(left, top, xres, yres), width, height)
    mosaic = categorical_tiles_on_grid([(path.stem, path) for path in paths], grid)
    return grid, np.where(mosaic.valid, mosaic.values, 0), mosaic.valid


def landcover_areas(tiles, aoi_path) -> tuple[dict[int, float], float, float]:
    """Return ({class: m²}, classified m², AOI m²) inside the AOI, exact equal-area weights.

    All classified cells count, whatever any Sentinel-2 mask says.
    """
    from src.geo.change_analysis import load_equal_area_aoi, prepare_pixel_area_weights

    grid, codes, valid = _landcover_grid(tiles)
    aoi = load_equal_area_aoi(aoi_path)
    weights = prepare_pixel_area_weights(grid, aoi)
    inside = valid & (weights > 0)
    areas = {int(code): float(weights[inside & (codes == code)].sum(dtype=np.float64))
             for code in np.unique(codes[inside])}
    return areas, float(weights[inside].sum(dtype=np.float64)), float(aoi.area_m2)
