"""Separate storage of layer files (rasters, vector snapshots) and their metadata.

* :class:`LayerFileStore` keeps immutable files under ``data/external/<source>/``
  addressed by a relative *storage key*. Files are published atomically and are
  never overwritten: a refresh produces a new key.
* :class:`FileLayerRepository` stores one JSON record per descriptor
  (``catalogue/<fingerprint>.json``, write-once) plus the storage key. It
  implements the ``LayerRepository`` protocol so a PostGIS table
  (``external_layers`` + ``storage_key`` column, GiST index on the extent) can
  replace it without changing the engine; see ``docs/multisource_demo.md``.

Records never contain absolute paths: keys are relative to the store root.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile

from src.data.external_sources import (
    ExternalLayerDescriptor, _utc, assess_temporal_join, descriptor_from_record, descriptor_to_record,
)


def publish_new_file(temporary: Path, final: Path) -> Path:
    """Move ``temporary`` to ``final`` atomically; fail if ``final`` already exists."""
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(temporary, final)  # Atomic and refuses an existing destination.
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError:
        if os.name != "nt":
            raise
        os.rename(temporary, final)  # Windows rename also refuses an existing destination.
        return final
    temporary.unlink()
    return final


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LayerFileStore:
    """Immutable files below one root, addressed by relative POSIX keys."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def path(self, key: str) -> Path:
        parts = PurePosixPath(key).parts
        if not parts or PurePosixPath(key).is_absolute() or ".." in parts or "\\" in key or ":" in key:
            raise ValueError("Clé de stockage invalide.")
        path = (self.root / Path(*parts)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Clé de stockage hors du dépôt.")
        return path

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def temporary(self, key: str) -> Path:
        """Temporary sibling for writers (GDAL, JSON) before :meth:`publish`."""
        final = self.path(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".tmp-", suffix=final.suffix, dir=final.parent)
        os.close(handle)
        return Path(name)

    def publish(self, temporary: Path, key: str) -> Path:
        return publish_new_file(Path(temporary), self.path(key))

    def write_bytes(self, key: str, payload: bytes) -> Path:
        temporary = self.temporary(key)
        try:
            temporary.write_bytes(payload)
            return self.publish(temporary, key)
        finally:
            temporary.unlink(missing_ok=True)

    def keys(self, prefix: str = "") -> tuple[str, ...]:
        folder = self.root if prefix in ("", ".") else self.path(prefix)
        if not folder.is_dir():
            return ()
        return tuple(sorted(path.relative_to(self.root).as_posix() for path in folder.rglob("*")
                            if path.is_file() and not path.name.startswith(".tmp-")))


class FileLayerRepository:
    """Write-once JSON metadata catalogue; one record per descriptor fingerprint."""

    def __init__(self, store: LayerFileStore, folder: str = "catalogue"):
        self.store, self.folder = store, folder

    def save(self, descriptor: ExternalLayerDescriptor, storage_key: str | None = None) -> str:
        if storage_key is not None:
            self.store.path(storage_key)  # Validates the key; never an absolute path.
        key = f"{self.folder}/{descriptor.fingerprint}.json"
        if not self.store.exists(key):
            record = {"descriptor": descriptor_to_record(descriptor), "storage_key": storage_key}
            self.store.write_bytes(key, json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True)
                                   .encode("utf-8"))
        return key

    def records(self) -> tuple[tuple[ExternalLayerDescriptor, str | None], ...]:
        output = []
        for key in self.store.keys(self.folder):
            if not key.endswith(".json"):
                continue
            record = json.loads(self.store.path(key).read_text(encoding="utf-8"))
            output.append((descriptor_from_record(record["descriptor"]), record.get("storage_key")))
        return tuple(output)

    def find(self, source_id: str, wgs84_bounds, observed_at: datetime | None = None):
        return tuple(descriptor for descriptor, _ in self.find_with_storage(source_id, wgs84_bounds, observed_at))

    def find_with_storage(self, source_id: str, wgs84_bounds, observed_at: datetime | None = None):
        """Equivalent of ``ST_Intersects(extent, bbox)`` ordered by temporal offset."""
        left, bottom, right, top = wgs84_bounds
        matches = []
        for descriptor, key in self.records():
            b = descriptor.extent.wgs84_bounds
            if descriptor.source_id != source_id or b[0] >= right or b[2] <= left or b[1] >= top or b[3] <= bottom:
                continue
            matches.append((descriptor, key))
        if observed_at is not None:
            instant = _utc(observed_at, "Observation")
            matches.sort(key=lambda item: (assess_temporal_join(item[0].temporal, instant, float("inf")).offset_days,
                                           -item[0].temporal.end_utc.timestamp()))
        return tuple(matches)
