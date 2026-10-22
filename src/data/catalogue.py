"""Immutable acquisition catalogue contracts, without UI or storage dependency."""

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
import math
from typing import Protocol


class CatalogueError(ValueError):
    """Public catalogue error; no source paths, credentials or transport details."""


@dataclass(frozen=True)
class CatalogueAOI:
    aoi_id: str
    bbox: tuple[float, float, float, float]
    geometry_json: str  # Canonical WGS84 GeoJSON geometry, not a mutable dict.
    sha256: str


@dataclass(frozen=True)
class CatalogueQuery:
    aoi: CatalogueAOI
    start: date
    end: date
    max_cloud_cover: float | None = None
    profile_identity: tuple[str, str, str] | None = None  # id, version, full fingerprint

    def __post_init__(self):
        if (not isinstance(self.aoi, CatalogueAOI) or
                any(not isinstance(day, date) or isinstance(day, datetime) for day in (self.start, self.end)) or
                self.start > self.end):
            raise ValueError("AOI ou période de recherche invalide.")
        cloud = self.max_cloud_cover
        if cloud is not None and (isinstance(cloud, bool) or not isinstance(cloud, (int, float))
                                  or not math.isfinite(cloud) or not 0 <= cloud <= 100):
            raise ValueError("Le plafond nuageux doit être compris entre 0 et 100 %.")

    @property
    def fingerprint(self):
        payload = asdict(self)
        payload.update(start=self.start.isoformat(), end=self.end.isoformat())
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class CatalogueRecord:
    collection: str
    stac_id: str
    acquired_at_utc: datetime
    global_cloud_cover: float | None
    b04_available: bool
    b08_available: bool
    scl_available: bool
    local_state: str = "absent"
    ingested_at_utc: datetime | None = None

    @property
    def identity(self):
        return self.collection, self.stac_id


@dataclass(frozen=True)
class CatalogueResult:
    query: CatalogueQuery
    records: tuple[CatalogueRecord, ...]
    complete: bool
    raw_count: int
    source: str
    searched_at_utc: datetime


class CatalogueRepository(Protocol):
    """Read interface implementable by STAC or, later, a PostGIS adapter.

    Identity is (collection, stac_id); acquisition and ingestion dates differ.
    Implementations must report completeness and never silently return a page
    as a complete search. Queries carry WGS84 geometry, UTC dates and AOI revision.
    """

    def search(self, query: CatalogueQuery) -> CatalogueResult: ...
