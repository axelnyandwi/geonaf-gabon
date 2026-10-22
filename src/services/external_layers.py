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


def latest_osm(repository: FileLayerRepository, wgs84_bounds) -> StoredLayer | None:
    """Most recent OSM snapshot whose recorded extent equals the AOI bounds."""
    candidates = [layer for layer in stored_layers(repository, "openstreetmap", wgs84_bounds)
                  if all(abs(a - b) < 1e-6 for a, b in zip(layer.descriptor.extent.wgs84_bounds, wgs84_bounds))]
    # Prefer records carrying a file checksum (verified before use) for the same snapshot.
    return max(candidates, key=lambda layer: (layer.descriptor.temporal.end_utc,
                                              layer.descriptor.quality.checksum_sha256 is not None), default=None)
