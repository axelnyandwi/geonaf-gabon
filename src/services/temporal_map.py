"""Versioned public display copies of an existing temporal analysis.

No raster reads, NDVI calculation, classification or statistics occur here.
AOI rasterization follows the existing helper; all images share one warp grid.
"""

import base64
import hashlib
import json
from collections import OrderedDict
from io import BytesIO
from math import ceil
from threading import RLock

from affine import Affine
import geopandas as gpd
from matplotlib import colormaps
import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject

from src.geo.change_analysis import _rasterize_aoi_pixel_mask


SCHEMA_VERSION = "geonaf.temporal-map/v1"
RENDER_VERSION = "nearest-rgba-v2-legend"
MAX_DISPLAY_SIZE = 2048
MAX_WORKING_BYTES = 256 * 1024**2
MAX_PAYLOAD_BYTES = 32 * 1024**2
MASKED_RGBA = (209, 213, 219, 255)
CLASS_COLORS = {1: "#c0392b", 2: "#f5f5bf", 3: "#238443"}


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("ascii")


def _png(values):
    output = BytesIO()
    Image.fromarray(values).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _legend(low, high):
    return [{"value": float(value), "color": "#" + "".join(f"{c:02x}" for c in
             colormaps["RdYlGn"](float((value-low)/(high-low)), bytes=True)[:3])}
            for value in np.linspace(low, high, 9)]


def _public_acquisition(acquisition):
    # Explicit allowlist: never serialize asset paths, XML, quality dictionaries
    # or arbitrary provenance attributes into a browser payload.
    calibration = acquisition.calibration
    return {"product_id": acquisition.product_id,
            "acquired_at_utc": acquisition.acquired_at_utc.isoformat(),
            "calibration": {name: float(getattr(calibration, name)) for name in
                            ("red_offset", "nir_offset", "quantification_value")}}


def prepare_temporal_map(result, *, max_size=1536):
    """Return geonaf.temporal-map/v1; source arrays are never modified.

    The memory guard bounds a conservative incremental array estimate, excluding
    pre-existing scientific arrays, native-library buffers and Python overhead.
    Oversized grids fail explicitly rather than silently lowering display size.
    """
    if isinstance(max_size, bool) or not isinstance(max_size, int) or not 1 <= max_size <= MAX_DISPLAY_SIZE:
        raise ValueError("Taille d’affichage attendue entre 1 et 2048 pixels.")
    if result.scl_policy != "usable_land" or result.aoi_path is None:
        raise ValueError("Une AOI et la politique usable_land sont requises.")
    grid = result.common_grid
    comparison, analysis = result.comparison, result.analysis
    shape = (grid.height, grid.width)
    arrays = (comparison.ndvi_date_1, comparison.ndvi_date_2,
              comparison.valid_date_1, comparison.valid_date_2,
              analysis.delta_ndvi, analysis.comparable_mask, analysis.change_class_map)
    if any(np.shape(a) != shape for a in arrays):
        raise ValueError("Tableaux incompatibles avec la grille scientifique.")
    if not np.isfinite(result.threshold) or result.threshold <= 0 or result.threshold != analysis.threshold:
        raise ValueError("Seuil scientifique incohérent.")
    # Mercator has a finite useful latitude domain; no clipping without notice.
    geographic = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    sl, sb, sr, st = grid.bounds
    footprint = [list(geographic.transform(x, y)) for x, y in
                 ((sl, st), (sr, st), (sr, sb), (sl, sb), (sl, st))]
    if any(not np.isfinite(p).all() or abs(p[1]) > 85.05112878 for p in footprint):
        raise ValueError("Emprise hors du domaine Mercator pris en charge.")
    if max(p[0] for p in footprint)-min(p[0] for p in footprint) > 180:
        raise ValueError("Emprise traversant l’antiméridien non prise en charge.")
    transform, width, height = calculate_default_transform(
        grid.crs, "EPSG:3857", grid.width, grid.height, *grid.bounds)
    factor = max(1, max(width, height)/max_size)
    new_width, new_height = ceil(width/factor), ceil(height/factor)
    transform = transform @ Affine.scale(width/new_width, height/new_height)
    width, height = new_width, new_height
    estimated_bytes = grid.width*grid.height*8 + width*height*96
    if estimated_bytes > MAX_WORKING_BYTES:
        raise ValueError("Budget mémoire de préparation dépassé ; découpage requis.")
    inside = _rasterize_aoi_pixel_mask(grid, result.aoi_path)

    def warp(source, *, categorical=False):
        destination = np.zeros((height, width), dtype=np.uint8) if categorical else np.full(
            (height, width), np.nan, dtype=np.float32)
        reproject(source=source, destination=destination, src_transform=grid.transform,
                  src_crs=grid.crs, dst_transform=transform, dst_crs="EPSG:3857",
                  resampling=Resampling.nearest)
        return destination

    masks = {}
    layers = []
    # Use the same 256-entry lookup table as the raster, not CSS interpolation
    # through a small set of sampled colours. This is display metadata only.
    legend_image = _png(colormaps['RdYlGn'](np.linspace(0, 1, 256), bytes=True)[None, :, :])
    definitions = (
        ("ndvi_initial", comparison.ndvi_date_1, comparison.valid_date_1, "initial", -1, 1),
        ("ndvi_final", comparison.ndvi_date_2, comparison.valid_date_2, "final", -1, 1),
        ("delta", analysis.delta_ndvi, analysis.comparable_mask, "comparable", -2, 2),
        ("classes", analysis.change_class_map, analysis.comparable_mask, "comparable", None, None),
    )
    display_categories = {}
    for layer_id, values, valid, mask_id, low, high in definitions:
        if mask_id not in display_categories:
            categories = np.where(inside, 1, 0).astype(np.uint8)
            categories[inside & valid & np.isfinite(values)] = 2
            display_categories[mask_id] = warp(categories, categorical=True)
            masks[mask_id] = _png(display_categories[mask_id])
        category = display_categories[mask_id]
        displayed = warp(values, categorical=layer_id == "classes")
        if layer_id == "classes":
            rgba = np.zeros((height, width, 4), dtype=np.uint8)
            for code, color in CLASS_COLORS.items():
                rgba[displayed == code] = (*bytes.fromhex(color[1:]), 255)
            legend = [{"code": code, "label": label, "color": CLASS_COLORS[code]}
                      for code, label in ((1, "decrease"), (2, "stable"), (3, "increase"))]
        else:
            normalized = np.clip((np.nan_to_num(displayed, nan=low)-low)/(high-low), 0, 1)
            rgba = colormaps["RdYlGn"](normalized, bytes=True)
            legend = _legend(low, high)
        rgba[category == 1] = MASKED_RGBA
        rgba[category == 0] = (0, 0, 0, 0)
        layers.append({"id": layer_id, "kind": "categorical" if low is None else "continuous",
                       "mask_id": mask_id, "image": _png(rgba), "legend": legend,
                       "provenance_keys": (["initial"] if layer_id == "ndvi_initial" else
                                           ["final"] if layer_id == "ndvi_final" else ["initial", "final"]),
                       "range": None if low is None else [low, high]})
        if low is not None:
            layers[-1]['legend_image'] = legend_image

    left, bottom, right, top = array_bounds(height, width, transform)
    to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    corners = [list(to_wgs84.transform(x, y)) for x, y in
               ((left, top), (right, top), (right, bottom), (left, bottom))]
    aoi = gpd.read_file(result.aoi_path).to_crs("EPSG:4326")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "grid": {"width": width, "height": height, "coordinates": corners,
                 "transform": list(transform)[:6], "crs": "EPSG:3857"},
        "layers": layers, "masks": masks,
        "aoi": {"type": "Feature", "properties": {}, "geometry": aoi.geometry.union_all().__geo_interface__},
        "footprint": {"type": "Feature", "properties": {},
                      "geometry": {"type": "Polygon", "coordinates": [footprint]}},
        "mask_policy": {"scl_policy": result.scl_policy, "scl_classes": [4, 5],
                        "categories": {"0": "exterior", "1": "unavailable", "2": "valid"},
                        "aoi_pixel_rule": "all_touched", "masked_rgba": list(MASKED_RGBA)},
        "classification": {"threshold": float(result.threshold), "delta_order": "final-initial",
                           "rules": {"1": "delta < -threshold", "2": "-threshold <= delta <= threshold",
                                     "3": "delta > threshold", "0": "non_comparable"}},
        "provenance": {"initial": _public_acquisition(result.earlier),
                       "final": _public_acquisition(result.later),
                       "source_grid": {"crs": str(grid.crs), "width": grid.width,
                                       "height": grid.height, "transform": list(grid.transform)[:6],
                                       "resolution": list(grid.resolution)},
                       "coverage_denominator": analysis.coverage_denominator,
                       "excluded_out_of_range_pixels": analysis.excluded_out_of_range_pixels},
        "display": {"max_size": max_size, "resampling": "nearest", "quantitative": False,
                    "render_version": RENDER_VERSION, "estimated_working_bytes": estimated_bytes},
    }
    # Stable content identity, independent of timings, file paths and cache keys.
    payload["result_id"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
    if len(_json_bytes(payload)) > MAX_PAYLOAD_BYTES:
        raise ValueError("Charge utile supérieure au budget autorisé.")
    return payload


class TemporalMapCache:
    """Process-local LRU of immutable JSON bytes, at most 4 entries / 64 MiB.

    scientific_key must cover acquisitions, input revisions, AOI, grid, SCL,
    calibration, threshold and analysis version (e.g. local_preview_key).
    Hits return independent dictionaries. Preparation is serialized to avoid
    concurrent duplicate allocations. No scientific result is retained.
    """

    def __init__(self, *, max_entries=4, max_bytes=64*1024**2):
        if (isinstance(max_entries, bool) or not isinstance(max_entries, int)
                or not 1 <= max_entries <= 4 or isinstance(max_bytes, bool)
                or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 64*1024**2):
            raise ValueError("Limites LRU invalides.")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries = OrderedDict()
        self._bytes = self._hits = self._misses = 0
        self._lock = RLock()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = self._hits = self._misses = 0

    def info(self):
        with self._lock:
            return {"entries": len(self._entries), "bytes": self._bytes,
                    "hits": self._hits, "misses": self._misses,
                    "max_entries": self.max_entries, "max_bytes": self.max_bytes}

    def prepare(self, result, *, scientific_key, max_size=1536):
        if scientific_key is None:
            raise ValueError("Une révision scientifique explicite est requise.")
        # Include display-relevant metadata independently of caller revisions.
        # The caller still owns revisions of the large input scientific arrays.
        from pathlib import Path
        aoi_path = Path(result.aoi_path)
        if aoi_path.stat().st_size > 8*1024**2:
            raise ValueError("AOI supérieure au budget du cache local.")
        aoi_digest = hashlib.sha256(aoi_path.read_bytes()).hexdigest()
        grid = result.common_grid
        key = hashlib.sha256(_json_bytes([
            SCHEMA_VERSION, RENDER_VERSION, scientific_key, max_size, aoi_digest,
            str(grid.crs), list(grid.transform)[:6], grid.width, grid.height,
            result.scl_policy, result.threshold, result.earlier.product_id,
            result.later.product_id])).digest()
        with self._lock:
            encoded = self._entries.pop(key, None)
            if encoded is not None:
                self._hits += 1
                self._entries[key] = encoded
            else:
                self._misses += 1
                encoded = _json_bytes(prepare_temporal_map(result, max_size=max_size))
                # Count retained key bytes as well as serialized payload bytes.
                size = len(key) + len(encoded)
                if size <= self.max_bytes:
                    while self._entries and (len(self._entries) >= self.max_entries
                                              or self._bytes+size > self.max_bytes):
                        old_key, old_value = self._entries.popitem(last=False)
                        self._bytes -= len(old_key)+len(old_value)
                    self._entries[key] = encoded
                    self._bytes += size
            return json.loads(encoded)
