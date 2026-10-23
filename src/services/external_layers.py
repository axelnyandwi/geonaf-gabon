"""Offline inventory of stored external layers for one AOI (no network)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.data.external_sources import ExternalLayerDescriptor
from src.data.layer_store import FileLayerRepository, LayerFileStore, sha256_file


@dataclass(frozen=True)
class StoredLayer:
    descriptor: ExternalLayerDescriptor
    storage_key: str
    path: Path


def open_store(data_dir: Path) -> tuple[LayerFileStore, FileLayerRepository]:
    store = LayerFileStore(Path(data_dir) / "external")
    return store, FileLayerRepository(store)


def stored_layers(repository: FileLayerRepository, source_id: str, wgs84_bounds, observed_at=None,
                  *, verify: bool = True) -> tuple[StoredLayer, ...]:
    """Layers intersecting the AOI, closest validity first; files must exist and match their digest."""
    output = []
    for descriptor, key in repository.find_with_storage(source_id, wgs84_bounds, observed_at):
        if key is None or not repository.store.exists(key):
            continue
        path = repository.store.path(key)
        digest = descriptor.quality.checksum_sha256
        if verify and digest is not None and sha256_file(path) != digest:
            raise ValueError("Fichier de couche externe modifié depuis son enregistrement.")
        output.append(StoredLayer(descriptor, key, path))
    return tuple(output)


def window_pixel_degrees(path: Path) -> float:
    """One cell of a stored raster window, in degrees (coverage tolerance)."""
    import rasterio

    with rasterio.open(path) as source:
        size = max(abs(source.res[0]), abs(source.res[1]))
        return size if source.crs.is_geographic else size / 111_320.0


def _extents_cover(layers, wgs84_bounds) -> bool:
    """Union of the windows' WGS84 extents covers the AOI bounds, within one cell."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    if not layers:
        return False
    tolerance = max(window_pixel_degrees(layer.path) for layer in layers)
    union = unary_union([box(*layer.descriptor.extent.wgs84_bounds) for layer in layers])
    return union.buffer(tolerance, join_style=2).covers(box(*wgs84_bounds))


def layers_for_aoi(layers, identity) -> tuple[StoredLayer, ...]:
    """Stored windows usable for this study zone (``identity``: ``AOIIdentity``); () otherwise.

    * only windows retrieved under the same zone identifier (adjacent zones overlap);
    * windows recorded for this exact geometry fingerprint are preferred;
    * windows recorded for **another** geometry are never used;
    * windows from before fingerprints (no record) are used only if, together,
      they cover the zone's bounds within one cell — historical data stay valid;
    * the selected windows must cover the zone, or nothing is returned and the
      source must be retrieved again for the current geometry.
    """
    own, seen = [], set()
    for layer in layers:
        if layer.descriptor.provenance.get("aoi_id") == identity.aoi_id and layer.storage_key not in seen:
            seen.add(layer.storage_key)
            own.append(layer)
    exact = [l for l in own if l.descriptor.provenance.get("aoi_geometry_sha256") == identity.geometry_sha256]
    legacy = [l for l in own if "aoi_geometry_sha256" not in l.descriptor.provenance]
    for candidates in (exact, legacy):
        if candidates and _extents_cover(candidates, identity.wgs84_bounds):
            return tuple(candidates)
    return ()


def latest_osm(repository: FileLayerRepository, wgs84_bounds) -> StoredLayer | None:
    """Most recent OSM snapshot whose recorded extent equals the AOI bounds."""
    candidates = [layer for layer in stored_layers(repository, "openstreetmap", wgs84_bounds)
                  if all(abs(a - b) < 1e-6 for a, b in zip(layer.descriptor.extent.wgs84_bounds, wgs84_bounds))]
    # Prefer records carrying a file checksum (verified before use) for the same snapshot.
    return max(candidates, key=lambda layer: (layer.descriptor.temporal.end_utc,
                                              layer.descriptor.quality.checksum_sha256 is not None), default=None)
