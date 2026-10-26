"""Lossless spatial subsets of verified Sentinel-2 assets, with explicit lineage.

A subset keeps the native pixel grid: the window is snapped to whole 20 m
cells measured from the tile origin, so it is also whole in the 10 m grid and
B04/B08/SCL stay mutually aligned. Pixels are copied without resampling or
type conversion (JPEG 2000 → GeoTIFF DEFLATE, both lossless), then re-read and
compared to the source window. Cropping changes file digests: the subset
manifest therefore records both the parent digests (verified against STAC at
ingestion) and the subset digests, never one in place of the other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import ceil, floor, isclose
from pathlib import Path
from typing import Mapping
import hashlib
import json

import numpy as np
import rasterio
from rasterio.windows import Window

SUBSET_SCHEMA_VERSION = "geonaf.sentinel2-subset/v1"
RASTER_ROLES = ("b04", "b08", "scl")
COPIED_ROLES = ("calibration_xml",)
ALIGNMENT_M = 20.0
GTIFF_OPTIONS = {"driver": "GTiff", "compress": "deflate", "predictor": 2, "tiled": True,
                 "blockxsize": 256, "blockysize": 256}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def aligned_subset_bounds(aoi_bounds, tile_transform, tile_size, *, margin_m: float,
                          alignment_m: float = ALIGNMENT_M) -> tuple[float, float, float, float]:
    """AOI bounds (tile CRS) + margin, snapped outward to the ``alignment_m`` grid of the tile.

    The result is clipped to the tile footprint; it is a multiple of every
    native resolution dividing ``alignment_m`` (10 m and 20 m for 20 m).
    """
    if margin_m < 0 or alignment_m <= 0:
        raise ValueError("Marge ou pas d'alignement invalide.")
    origin_x, origin_y = tile_transform.c, tile_transform.f
    width, height = tile_size
    tile_right = origin_x + width * tile_transform.a
    tile_bottom = origin_y + height * tile_transform.e
    left, bottom, right, top = aoi_bounds
    left = origin_x + floor((left - margin_m - origin_x) / alignment_m) * alignment_m
    right = origin_x + ceil((right + margin_m - origin_x) / alignment_m) * alignment_m
    top = origin_y - floor((origin_y - top - margin_m) / alignment_m) * alignment_m
    bottom = origin_y - ceil((origin_y - bottom + margin_m) / alignment_m) * alignment_m
    bounds = (max(left, origin_x), max(bottom, tile_bottom), min(right, tile_right), min(top, origin_y))
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError("L'AOI n'intersecte pas la tuile.")
    return bounds


def integer_window(transform, bounds) -> Window:
    """Exact integer window of ``bounds``; refuses any fractional pixel offset."""
    left, bottom, right, top = bounds
    values = ((left - transform.c) / transform.a, (top - transform.f) / transform.e,
              (right - left) / transform.a, (bottom - top) / transform.e)
    rounded = tuple(round(value) for value in values)
    if any(not isclose(value, whole, abs_tol=1e-9) for value, whole in zip(values, rounded)):
        raise ValueError("Emprise non alignée sur la grille native.")
    col, row, width, height = rounded
    if width <= 0 or height <= 0:
        raise ValueError("Fenêtre vide.")
    return Window(col, row, width, height)


def write_raster_subset(source_path: Path, destination: Path, bounds, *, product_id: str) -> dict:
    """Copy one native-grid window losslessly and prove pixel equality by re-reading it."""
    with rasterio.open(source_path) as source:
        if source.count != 1 or source.crs is None:
            raise ValueError("Raster source mono-bande avec CRS requis.")
        window = integer_window(source.transform, bounds)
        if (window.col_off < 0 or window.row_off < 0 or window.col_off + window.width > source.width
                or window.row_off + window.height > source.height):
            raise ValueError("Fenêtre hors de la tuile source.")
        values = source.read(1, window=window)
        profile = {**GTIFF_OPTIONS, "dtype": source.dtypes[0], "count": 1, "crs": source.crs,
                   "transform": source.window_transform(window), "width": int(window.width),
                   "height": int(window.height), "nodata": source.nodata}
        source_crs = source.crs
    destination.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(destination, "w", **profile) as target:
        target.write(values, 1)
        target.update_tags(product_id=product_id)
    with rasterio.open(destination) as written:
        if (written.crs != source_crs or written.transform != profile["transform"]
                or written.dtypes[0] != profile["dtype"] or written.nodata != profile["nodata"]
                or not np.array_equal(written.read(1), values)):
            raise ValueError("L'extrait relu diffère de la fenêtre source.")
    transform = profile["transform"]
    return {
        "path": destination.name,
        "sha256": sha256_file(destination),
        "crs": source_crs.to_string(),
        "dtype": profile["dtype"],
        "nodata": profile["nodata"],
        "resolution": [abs(transform.a), abs(transform.e)],
        "transform": [transform.a, transform.b, transform.c, transform.d, transform.e, transform.f],
        "extent": [bounds[0], bounds[1], bounds[2], bounds[3]],
        "width": int(window.width),
        "height": int(window.height),
        "source_window": {"col_off": int(window.col_off), "row_off": int(window.row_off),
                          "width": int(window.width), "height": int(window.height)},
        "pixel_equality": "verified",
    }


def build_subset_manifest(*, parent_manifest: Mapping, parent_manifest_sha256: str, assets: Mapping,
                          aoi: Mapping, margin_m: float, created_at: datetime, software: Mapping,
                          quality: Mapping | None = None) -> dict:
    """Assemble and validate the lineage record of one subset acquisition."""
    parent_assets = parent_manifest["assets"]
    manifest = {
        "schema_version": SUBSET_SCHEMA_VERSION,
        "product_id": parent_manifest["product_id"],
        "acquired_at_utc": parent_manifest["acquired_at_utc"],
        "collection": parent_manifest["collection"],
        "stac": dict(parent_manifest["stac"]),
        "calibration": dict(parent_manifest["calibration"]),
        "parent": {
            "schema_version": parent_manifest["schema_version"],
            "manifest_path": "source_acquisition_manifest.json",
            "manifest_sha256": parent_manifest_sha256,
            "ingested_at_utc": parent_manifest["ingested_at_utc"],
            "integrity": parent_manifest["integrity"],
            "assets": {role: {"path": record["path"], "sha256": record["sha256"], "id": record["id"]}
                       for role, record in parent_assets.items()},
        },
        "derivation": {
            "method": "native_grid_window_copy",
            "resampling": "none",
            "alignment_m": ALIGNMENT_M,
            "margin_m": float(margin_m),
            "aoi": dict(aoi),
            "created_at_utc": created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "software": dict(software),
            "format": {key: value for key, value in GTIFF_OPTIONS.items()},
        },
        "assets": {role: {**dict(record), "id": parent_assets[role]["id"],
                          "product_id": parent_assets[role]["product_id"]}
                   for role, record in assets.items()},
        "quality": dict(quality or {}),
    }
    validate_subset_manifest(manifest)
    return manifest


def _is_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_subset_manifest(manifest: Mapping) -> None:
    """Shape and lineage checks; file digests are verified separately by the reader."""
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SUBSET_SCHEMA_VERSION:
        raise ValueError("Version de schéma d'extrait non prise en charge.")
    product_id = manifest.get("product_id")
    parent = manifest.get("parent")
    assets = manifest.get("assets")
    if not isinstance(product_id, str) or not isinstance(parent, Mapping) or not isinstance(assets, Mapping):
        raise ValueError("Manifeste d'extrait incomplet.")
    if set(assets) != set(RASTER_ROLES + COPIED_ROLES) or set(parent.get("assets", {})) != set(assets):
        raise ValueError("L'extrait doit décrire B04, B08, SCL et le XML de calibration.")
    integrity = parent.get("integrity", {})
    if integrity.get("mode") != "strict" or any(
            record.get("status") != "verified" for record in integrity.get("source_assets", {}).values()):
        raise ValueError("La source de l'extrait n'a pas une intégrité stricte vérifiée.")
    if not _is_digest(parent.get("manifest_sha256")):
        raise ValueError("Empreinte du manifeste parent invalide.")
    for role, record in assets.items():
        path = record.get("path")
        if (not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts
                or len(Path(path).parts) != 1):
            raise ValueError("Chemin d'actif d'extrait invalide.")
        if record.get("product_id") != product_id or not _is_digest(record.get("sha256")):
            raise ValueError("Identité ou empreinte d'actif d'extrait invalide.")
        if not _is_digest(parent["assets"][role].get("sha256")):
            raise ValueError("Empreinte d'actif parent invalide.")
        if role in RASTER_ROLES and record.get("pixel_equality") != "verified":
            raise ValueError("Égalité des pixels de l'extrait non vérifiée.")
        if role in COPIED_ROLES and record.get("sha256") != parent["assets"][role]["sha256"]:
            raise ValueError("Un fichier copié doit conserver l'empreinte de sa source.")
    if manifest.get("derivation", {}).get("resampling") != "none":
        raise ValueError("Un extrait ne doit pas être rééchantillonné.")


def write_json(document: Mapping, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return destination
