"""Contract for external geospatial layers (land cover, OpenStreetMap, …).

No UI, no network and no storage dependency. An adapter describes a layer
(source, version, temporal validity, resolution, CRS, extent, licence,
provenance, quality) and reads already available local files. Retrieval plans
are explicit and bounded; executing them is a separate, authorised step.

The descriptor fields map one-to-one to a future PostGIS table
(``external_layers``) whose extent becomes a geometry column; see
``docs/multisource.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from types import MappingProxyType
from typing import Literal, Mapping, Protocol

LayerKind = Literal["categorical_raster", "continuous_raster", "vector"]
TemporalKind = Literal["instant", "interval", "snapshot"]


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} doit être daté avec un fuseau horaire.")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TemporalValidity:
    """When a layer describes the territory, not when it was downloaded.

    ``snapshot`` covers database extracts (e.g. OSM at a replication
    timestamp): start == end == snapshot instant. A land-cover map built from a
    year of imagery is an ``interval``.
    """

    kind: TemporalKind
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self):
        start, end = _utc(self.start_utc, "Début"), _utc(self.end_utc, "Fin")
        if start > end or (self.kind in ("instant", "snapshot") and start != end):
            raise ValueError("Validité temporelle incohérente.")
        if self.kind not in ("instant", "interval", "snapshot"):
            raise ValueError("Type de validité temporelle inconnu.")
        object.__setattr__(self, "start_utc", start)
        object.__setattr__(self, "end_utc", end)


@dataclass(frozen=True)
class SpatialExtent:
    """Rectangular extent in the layer CRS plus its WGS84 equivalent."""

    crs: str
    bounds: tuple[float, float, float, float]
    wgs84_bounds: tuple[float, float, float, float]

    def __post_init__(self):
        for box in (self.bounds, self.wgs84_bounds):
            if (len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError("Emprise invalide.")
        if not isinstance(self.crs, str) or not self.crs.strip():
            raise ValueError("CRS de l'emprise requis.")


@dataclass(frozen=True)
class LayerLicense:
    spdx_id: str  # e.g. "CC-BY-4.0", "ODbL-1.0"
    attribution: str
    url: str
    share_alike: bool

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.spdx_id, self.attribution, self.url)):
            raise ValueError("Licence, attribution et URL de licence sont obligatoires.")
        if not self.url.startswith("https://"):
            raise ValueError("URL de licence HTTPS requise.")


@dataclass(frozen=True)
class LayerQuality:
    """Declared or locally verified quality; never inferred silently."""

    status: Literal["declared", "verified", "unknown"]
    notes: tuple[str, ...] = ()
    checksum_sha256: str | None = None  # Local file digest when read from disk.


@dataclass(frozen=True)
class ExternalLayerDescriptor:
    source_id: str
    product: str
    version: str
    kind: LayerKind
    temporal: TemporalValidity
    resolution_m: float | None  # Nominal ground resolution; None for vectors.
    crs: str
    extent: SpatialExtent
    license: LayerLicense
    provenance: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    quality: LayerQuality = LayerQuality("unknown")
    classes: Mapping[int, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self):
        if self.kind not in ("categorical_raster", "continuous_raster", "vector"):
            raise ValueError("Type de couche inconnu.")
        if self.kind == "vector" and self.resolution_m is not None:
            raise ValueError("Une couche vecteur n'a pas de résolution raster.")
        if self.kind != "vector" and (self.resolution_m is None or not math.isfinite(self.resolution_m)
                                      or self.resolution_m <= 0):
            raise ValueError("Résolution raster positive requise.")
        if any(any(marker in str(value) for marker in ("\\", "file:")) or str(value).startswith("/")
               for value in self.provenance.values()):
            raise ValueError("La provenance ne doit pas contenir de chemin local.")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        object.__setattr__(self, "classes", MappingProxyType(dict(self.classes)))

    @property
    def identity(self) -> tuple[str, str]:
        return self.source_id, self.version

    @property
    def fingerprint(self) -> str:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for name in ("temporal", "extent", "license", "quality"):
            payload[name] = asdict(payload[name])
        payload["provenance"], payload["classes"] = dict(self.provenance), {str(k): v for k, v in self.classes.items()}
        encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class RetrievalPlan:
    """Explicit list of resources an authorised step may fetch; nothing is fetched here."""

    source_id: str
    resources: tuple[str, ...]  # HTTPS URLs or query texts, never credentials.
    estimated_bytes: int | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemporalJoinAssessment:
    """Relation between an observation instant and a layer validity."""

    relation: Literal["within", "before_layer", "after_layer"]
    offset_days: float
    within_tolerance: bool
    message: str


def assess_temporal_join(layer: TemporalValidity, observed_at: datetime,
                         tolerance_days: float) -> TemporalJoinAssessment:
    """Never assume a static layer holds at another date; report the offset."""
    if (isinstance(tolerance_days, bool) or not isinstance(tolerance_days, (int, float))
            or math.isnan(tolerance_days) or tolerance_days < 0):
        raise ValueError("Tolérance temporelle invalide.")
    instant = _utc(observed_at, "Observation")
    if layer.start_utc <= instant <= layer.end_utc:
        return TemporalJoinAssessment("within", 0.0, True, "Observation dans la période de validité de la couche.")
    if instant < layer.start_utc:
        offset, relation = (layer.start_utc - instant).total_seconds() / 86400, "before_layer"
    else:
        offset, relation = (instant - layer.end_utc).total_seconds() / 86400, "after_layer"
    ok = offset <= tolerance_days
    message = (f"Écart de {offset:.0f} jours avec la validité de la couche"
               + ("" if ok else " : l'occupation du sol a pu changer, jointure à interpréter avec prudence."))
    return TemporalJoinAssessment(relation, offset, ok, message)


class ExternalSourceAdapter(Protocol):
    """Adapter per source. Implementations must not perform hidden network I/O."""

    source_id: str

    def plan(self, wgs84_bounds: tuple[float, float, float, float]) -> RetrievalPlan: ...

    def describe_local(self, path, *, retrieved_at: datetime) -> ExternalLayerDescriptor: ...


class LayerRepository(Protocol):
    """Storage seam: in-memory now, PostGIS later (descriptor + geometry extent)."""

    def save(self, descriptor: ExternalLayerDescriptor) -> None: ...

    def find(self, source_id: str, wgs84_bounds, observed_at: datetime | None = None
             ) -> tuple[ExternalLayerDescriptor, ...]: ...


def descriptor_to_record(descriptor: ExternalLayerDescriptor) -> dict:
    """JSON-ready record (one row of the future ``external_layers`` table)."""
    record = {name: getattr(descriptor, name) for name in descriptor.__dataclass_fields__}
    for name in ("temporal", "extent", "license", "quality"):
        record[name] = asdict(record[name])
    record["temporal"]["start_utc"] = descriptor.temporal.start_utc.isoformat()
    record["temporal"]["end_utc"] = descriptor.temporal.end_utc.isoformat()
    record["quality"]["notes"] = list(descriptor.quality.notes)
    record["extent"]["bounds"] = list(descriptor.extent.bounds)
    record["extent"]["wgs84_bounds"] = list(descriptor.extent.wgs84_bounds)
    record["provenance"] = dict(descriptor.provenance)
    record["classes"] = {str(code): label for code, label in descriptor.classes.items()}
    record["fingerprint"] = descriptor.fingerprint
    return record


def descriptor_from_record(record: Mapping) -> ExternalLayerDescriptor:
    """Inverse of :func:`descriptor_to_record`; the fingerprint must match."""
    temporal = record["temporal"]
    extent = record["extent"]
    descriptor = ExternalLayerDescriptor(
        source_id=record["source_id"], product=record["product"], version=record["version"], kind=record["kind"],
        temporal=TemporalValidity(temporal["kind"], datetime.fromisoformat(temporal["start_utc"]),
                                  datetime.fromisoformat(temporal["end_utc"])),
        resolution_m=record["resolution_m"], crs=record["crs"],
        extent=SpatialExtent(extent["crs"], tuple(extent["bounds"]), tuple(extent["wgs84_bounds"])),
        license=LayerLicense(**record["license"]), provenance=dict(record["provenance"]),
        quality=LayerQuality(record["quality"]["status"], tuple(record["quality"]["notes"]),
                             record["quality"]["checksum_sha256"]),
        classes={int(code): label for code, label in record["classes"].items()})
    if "fingerprint" in record and record["fingerprint"] != descriptor.fingerprint:
        raise ValueError("Empreinte du descripteur incohérente : enregistrement modifié.")
    return descriptor


class InMemoryLayerRepository:
    def __init__(self):
        self._items: dict[tuple[str, str, str], ExternalLayerDescriptor] = {}

    def save(self, descriptor: ExternalLayerDescriptor) -> None:
        self._items[(*descriptor.identity, descriptor.fingerprint)] = descriptor

    def find(self, source_id, wgs84_bounds, observed_at=None):
        left, bottom, right, top = wgs84_bounds
        matches = []
        for item in self._items.values():
            b = item.extent.wgs84_bounds
            if item.source_id != source_id or b[0] >= right or b[2] <= left or b[1] >= top or b[3] <= bottom:
                continue
            matches.append(item)
        if observed_at is not None:
            instant = _utc(observed_at, "Observation")
            # Closest validity first; ties keep the most recent version.
            matches.sort(key=lambda d: (assess_temporal_join(d.temporal, instant, math.inf).offset_days,
                                        -d.temporal.end_utc.timestamp()))
        return tuple(matches)
