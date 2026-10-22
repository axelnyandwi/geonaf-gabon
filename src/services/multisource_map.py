"""Public display payload for the multisource map; no Streamlit, no scientific computation.

All raster layers (NDVI, WorldCover, buildings, radar) are warped with the
nearest-neighbour rule onto one shared Web Mercator display grid derived from
the Sentinel-2 grid, so the browser receives images with identical corners.
Statistics are never recomputed here. Only public metadata leaves the module
(no local paths, no storage keys).
"""

from __future__ import annotations

import base64
from io import BytesIO
from math import ceil
from time import perf_counter

from affine import Affine
import geopandas as gpd
from matplotlib import colormaps
import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject

from src.services.sentinel1_layer import DISPLAY_RANGE_DB

SCHEMA_VERSION = "geonaf.multisource-map/v1"
MAX_VECTOR_FEATURES = 20_000
# Official ESA WorldCover v200 palette.
WORLDCOVER_COLORS = {10: "#006400", 20: "#ffbb22", 30: "#ffff4c", 40: "#f096ff", 50: "#fa0000", 60: "#b4b4b4",
                     70: "#f0f0f0", 80: "#0064c8", 90: "#0096a0", 95: "#00cf75", 100: "#fae6a0"}
OSM_COLORS = {"Bâtiments": "#f97316", "Routes et voies": "#facc15", "Eau (OSM)": "#38bdf8",
              "Zones humides (OSM)": "#2dd4bf", "Végétation (OSM)": "#22c55e", "Plages (OSM)": "#fde68a",
              "Usage du sol (OSM)": "#c084fc", "Autres (OSM)": "#e5e7eb"}
MASKED_RGBA = (209, 213, 219, 255)


def _hex(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _png(rgba):
    output = BytesIO()
    Image.fromarray(rgba).save(output, format="PNG", compress_level=6)
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _display_grid(grid, max_size):
    transform, width, height = calculate_default_transform(grid.crs, "EPSG:3857", grid.width, grid.height,
                                                           *grid.bounds)
    if max(width, height) > max_size:
        factor = max(width, height) / max_size
        new_width, new_height = ceil(width / factor), ceil(height / factor)
        transform = transform @ Affine.scale(width / new_width, height / new_height)
        width, height = new_width, new_height
    return transform, width, height


def _warp(array, grid, display, fill):
    transform, width, height = display
    output = np.full((height, width), fill, dtype=array.dtype)
    reproject(source=array, destination=output, src_transform=grid.transform, src_crs=grid.crs,
              dst_transform=transform, dst_crs="EPSG:3857", resampling=Resampling.nearest,
              src_nodata=None, dst_nodata=None, init_dest_nodata=False)
    return output


def _indexed_png(indices, palette):
    """Palette PNG: index 0 transparent. Display only; statistics keep native values."""
    image = Image.fromarray(indices.astype(np.uint8), mode="P")
    flat = [channel for color in palette for channel in color[:3]]
    image.putpalette(flat + [0] * (768 - len(flat)))
    output = BytesIO()
    image.save(output, format="PNG", transparency=0, compress_level=6)
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


NDVI_LEVELS = 254  # Index 0: exterior (transparent), 1: masked, 2..255: NDVI −1..+1 (step ≈ 0.008).
NDVI_PALETTE = [(0, 0, 0), MASKED_RGBA[:3]] + [
    tuple(int(c) for c in colormaps["RdYlGn"](i / (NDVI_LEVELS - 1), bytes=True)[:3]) for i in range(NDVI_LEVELS)]
GRAY_PALETTE = [(0, 0, 0)] + [(i, i, i) for i in np.linspace(0, 255, 255).astype(int)]


def _ndvi_png(observation, display):
    categories = np.where(observation.aoi_pixels, 1, 0).astype(np.uint8)
    categories[observation.valid] = 2
    values = _warp(np.nan_to_num(observation.ndvi, nan=0.0).astype(np.float32), observation.grid, display, 0.0)
    categories = _warp(categories, observation.grid, display, 0)
    indices = 2 + np.rint(np.clip((values + 1) / 2, 0, 1) * (NDVI_LEVELS - 1)).astype(np.int32)
    indices[categories == 1] = 1
    indices[categories == 0] = 0
    return _indexed_png(indices, NDVI_PALETTE)


def _worldcover_rgba(result, display, classes):
    codes = np.where(result.worldcover_valid & result.observation.aoi_pixels, result.worldcover_codes, 0)
    codes = _warp(codes.astype(np.uint16), result.observation.grid, display, 0)
    rgba = np.zeros(codes.shape + (4,), dtype=np.uint8)
    for code in classes:
        if code in WORLDCOVER_COLORS:
            rgba[codes == code] = (*_hex(WORLDCOVER_COLORS[code]), 255)
    return rgba


def _radar_png(layer, display):
    low, high = DISPLAY_RANGE_DB[layer.polarization]
    scaled = np.clip((np.nan_to_num(layer.db, nan=low) - low) / (high - low), 0, 1)
    indices = (1 + np.rint(scaled * 254)).astype(np.uint8)
    indices = _warp(np.where(layer.valid, indices, 0).astype(np.uint8), layer.grid, display, 0)
    return _indexed_png(indices, GRAY_PALETTE)


def _buildings_rgba(result, display):
    # Reuse the cells used for statistics (pixel centre on the S2 grid): what is shown is what is counted.
    mask = result.osm_masks["Bâtiments"].astype(np.uint8)
    burned = _warp(mask, result.observation.grid, display, 0)
    rgba = np.zeros(burned.shape + (4,), dtype=np.uint8)
    rgba[burned == 1] = (*_hex(OSM_COLORS["Bâtiments"]), 230)
    return rgba


def _vector_features(frame, groups, aoi_geometry):
    """Objects clipped to the AOI (as for statistics), simplified at 1e-5° (≈1.1 m)."""
    selected = frame[frame["group"].isin(groups) & (frame["group"] != "Bâtiments")]
    if len(selected) > MAX_VECTOR_FEATURES:
        raise ValueError("Trop d'objets vecteur pour l'affichage interactif.")
    clipped = selected.geometry.intersection(aoi_geometry).simplify(1e-5, preserve_topology=True)
    features = []
    for (_, row), geometry in zip(selected.iterrows(), clipped):
        if geometry is None or geometry.is_empty or geometry.geom_type == "GeometryCollection":
            continue
        shape = geometry.__geo_interface__
        features.append({"type": "Feature", "geometry": _round(shape),
                         "properties": {"group": row["group"], "category": row["category"],
                                        "detail": row["detail"] if isinstance(row["detail"], str) else "", "osm_id": row["osm_id"],
                                        "color": OSM_COLORS.get(row["group"], "#e5e7eb")}})
    return {"type": "FeatureCollection", "features": features}


def _round(shape):
    def walk(value):
        if isinstance(value, (list, tuple)):
            if value and isinstance(value[0], (int, float)):
                return [round(float(v), 5) for v in value]  # ≈1.1 m, matches the 1e-5° simplification.
            return [walk(item) for item in value]
        return value
    return {"type": shape["type"], "coordinates": walk(shape["coordinates"])}


def prepare_multisource_map(result, *, aoi_path, worldcover_classes=None, osm_groups=None,
                            radar_polarization=None, max_size: int = 1280) -> dict:
    """Payload for ``multisource.js``; filters only change what is drawn, never the statistics.

    ``None`` filters keep everything; ``radar_polarization="none"`` sends no radar layer.
    """
    started = perf_counter()
    if not isinstance(max_size, int) or isinstance(max_size, bool) or not 64 <= max_size <= 2048:
        raise ValueError("Taille d'affichage hors bornes.")
    observation = result.observation
    aoi_geometry = gpd.read_file(aoi_path).to_crs("EPSG:4326").geometry.union_all()
    display = _display_grid(observation.grid, max_size)
    transform, width, height = display
    left, bottom, right, top = array_bounds(height, width, transform)
    to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    corners = [list(to_wgs84.transform(x, y)) for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))]
    rasters = [{"id": "ndvi", "label": f"NDVI Sentinel-2 · {observation.acquired_at_utc:%d/%m/%Y}",
                "nature": "Observation mesurée", "image": _ndvi_png(observation, display),
                "visible": True, "opacity": 0.9,
                "legend": {"kind": "gradient", "low": "−1", "high": "+1",
                           "colors": ["#a50026", "#fdae61", "#ffffbf", "#a6d96a", "#006837"],
                           "extra": [{"color": "#d1d5db", "label": "Observation masquée (SCL, nuages, eau…)"}]}}]
    if result.worldcover is not None:
        present = [z.code for z in result.worldcover.zones if z.pixel_count]
        chosen = [code for code in present if worldcover_classes is None or code in worldcover_classes]
        labels = dict(result.worldcover.descriptor.classes)
        rasters.append({"id": "worldcover", "label": "ESA WorldCover 2021", "nature": "Classification externe",
                        "image": _png(_worldcover_rgba(result, display, chosen)), "visible": False, "opacity": 0.75,
                        "legend": {"kind": "classes", "items": [{"color": WORLDCOVER_COLORS[c], "label": labels[c]}
                                                                for c in chosen]}})
    for layer in result.radar:
        if radar_polarization is not None and layer.polarization != radar_polarization:
            continue
        low, high = DISPLAY_RANGE_DB[layer.polarization]
        rasters.append({"id": f"s1-{layer.polarization.lower()}",
                        "label": f"Sentinel-1 {layer.polarization} · {layer.descriptor.temporal.start_utc:%m/%Y}",
                        "nature": "Observation mesurée (radar)", "image": _radar_png(layer, display),
                        "visible": False, "opacity": 0.9,
                        "legend": {"kind": "gradient", "low": f"{low:g} dB", "high": f"{high:g} dB",
                                   "colors": ["#000000", "#ffffff"], "extra": []}})
    groups = sorted(set(result.osm_frame["group"])) if result.osm_frame is not None else []
    groups = [g for g in groups if osm_groups is None or g in osm_groups]
    vectors = None
    if result.osm_frame is not None:
        if "Bâtiments" in groups:
            rasters.append({"id": "osm-buildings", "label": "Bâtiments OSM (cellules 10 m)", "nature": "Base collaborative",
                            "image": _png(_buildings_rgba(result, display)),
                            "visible": True, "opacity": 0.85,
                            "legend": {"kind": "classes", "items": [{"color": OSM_COLORS["Bâtiments"],
                                                                     "label": "Cellule 10 m dont le centre est dans un bâtiment"}]}})
        vectors = _vector_features(result.osm_frame, groups, aoi_geometry)
    aoi = aoi_geometry.__geo_interface__
    attributions = sorted({source.attribution for source in result.sources})
    return {
        "schema_version": SCHEMA_VERSION, "coordinates": corners, "width": width, "height": height,
        "rasters": rasters, "vectors": vectors,
        "vector_legend": [{"color": OSM_COLORS.get(g, "#e5e7eb"), "label": g} for g in groups if g != "Bâtiments"],
        "aoi": {"type": "Feature", "properties": {}, "geometry": _round(aoi)},
        "attributions": attributions, "display_crs": "EPSG:3857",
        "note": "Affichage reprojeté au plus proche voisin ; statistiques calculées sur les grilles natives.",
        "preparation_ms": round((perf_counter() - started) * 1000, 1),
    }
