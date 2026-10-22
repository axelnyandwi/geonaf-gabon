"""Public map payload from a scientific result, without Streamlit or source writes."""

import base64
from io import BytesIO
from math import ceil
from time import perf_counter

import geopandas as gpd
from matplotlib import colormaps
import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject
from affine import Affine

from src.geo.change_analysis import _rasterize_aoi_pixel_mask
from src.geo.scientific_profile import public_scientific_metadata


def prepare_ndvi_layer(result, *, max_size=1536):
    """Nearest-neighbour display copy of date 2 on an axis-aligned Mercator grid.

    Categories 0/1/2 mean exterior/masked/valid. The same nearest-neighbour
    warp is applied to values and categories: no interpolation through masks.
    Image corners are returned clockwise from north-west in longitude/latitude.
    Only public geographic metadata and PNG bytes leave this module.
    """
    started = perf_counter()
    if result.scl_policy != "usable_land":
        raise ValueError("La carte quantitative requiert usable_land (SCL 4 et 5).")
    if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size < 1:
        raise ValueError("La taille d’aperçu doit être un entier positif.")
    if result.aoi_path is None:
        raise ValueError("Une AOI est requise pour distinguer les pixels extérieurs.")
    grid = result.common_grid
    values = np.asarray(result.comparison.ndvi_date_2, dtype=np.float32)
    inside = _rasterize_aoi_pixel_mask(grid, result.aoi_path)
    valid = inside & result.comparison.valid_date_2 & np.isfinite(values)
    categories = np.where(inside, 1, 0).astype(np.uint8)
    categories[valid] = 2
    transform, width, height = calculate_default_transform(
        grid.crs, "EPSG:3857", grid.width, grid.height, *grid.bounds,
    )
    if max(width, height) > max_size:
        factor = max(width, height) / max_size
        new_width, new_height = ceil(width / factor), ceil(height / factor)
        transform = transform @ Affine.scale(width / new_width, height / new_height)
        width, height = new_width, new_height
    display_values = np.full((height, width), np.nan, dtype=np.float32)
    display_categories = np.zeros((height, width), dtype=np.uint8)
    for source, destination in ((values, display_values), (categories, display_categories)):
        reproject(source=source, destination=destination,
                  src_transform=grid.transform, src_crs=grid.crs,
                  dst_transform=transform, dst_crs="EPSG:3857",
                  resampling=Resampling.nearest)
    normalized = np.clip((np.nan_to_num(display_values, nan=0.0) + 1) / 2, 0, 1)
    rgba = colormaps["RdYlGn"](normalized, bytes=True)
    rgba[display_categories == 1] = (209, 213, 219, 255)
    rgba[display_categories == 0] = (0, 0, 0, 0)
    output = BytesIO()
    Image.fromarray(rgba).save(output, format="PNG")
    left, bottom, right, top = array_bounds(height, width, transform)
    to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    corners = [list(to_wgs84.transform(x, y)) for x, y in
               ((left, top), (right, top), (right, bottom), (left, bottom))]
    aoi = gpd.read_file(result.aoi_path).to_crs("EPSG:4326")
    geometry = aoi.geometry.union_all().__geo_interface__
    source_to_wgs84 = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    sl, sb, sr, st = grid.bounds
    footprint = [list(source_to_wgs84.transform(x, y)) for x, y in
                 ((sl, st), (sr, st), (sr, sb), (sl, sb), (sl, st))]
    stops = []
    for value in np.linspace(-1, 1, 9):
        rgb = colormaps["RdYlGn"]((float(value) + 1) / 2, bytes=True)[:3]
        stops.append({"value": float(value), "color": "#" + "".join(f"{c:02x}" for c in rgb)})
    return {
        "schema_version": "geonaf.ndvi-map/v1",
        "image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
        "coordinates": corners, "width": width, "height": height,
        "aoi": {"type": "Feature", "properties": {}, "geometry": geometry},
        "footprint": {"type": "Feature", "properties": {},
                      "geometry": {"type": "Polygon", "coordinates": [footprint]}},
        "acquired_at": result.later.acquired_at_utc.isoformat(),
        "scl_policy": result.scl_policy, "source_crs": str(grid.crs),
        "display_crs": "EPSG:3857", "legend": stops,
        "scientific": public_scientific_metadata(result),
        "preparation_ms": (perf_counter() - started) * 1000,
    }
