"""Versioned, immutable registry of territories (study zones); storage only, no UI, no network.

A territory is what GEONAF observes. Its limits may be an acquisition square, a
community or humanitarian administrative referential, a user import or a cell
of a stable geographic grid. The registry records **which geometry** (normalized
fingerprint, ``src.geo.aoi_identity``), **where it comes from** (source,
licence, vintage) and **what it legally is** (``legal_status``), so that:

* an acquisition zone is never presented as an administrative unit;
* no limit is declared official without verifiable evidence;
* two referentials describing the same unit stay two distinct territories;
* a geometry change creates a new version; earlier versions stay readable.

Layout below ``data/territories/`` (write-once, never overwritten):

* ``geometries/<geometry_sha256>.geojson`` — the registered geometry file;
* ``records/<territory_id>/v<NNNN>.json`` — one record per version.

PostGIS mapping (future): table ``territories`` with primary key
``(territory_id, version)``, ``geom geometry(MultiPolygon, 4326)`` and the
record fields as columns; see ``docs/territory_registry.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from src.data.layer_store import LayerFileStore, sha256_file
from src.geo.aoi_identity import load_aoi_identity

SCHEMA = "geonaf.territory/v1"
ACQUISITION_ZONE, ADMIN_UNIT, CUSTOM_IMPORT, GRID_CELL = "acquisition_zone", "admin_unit", "custom_import", "grid_cell"
KINDS = (ACQUISITION_ZONE, ADMIN_UNIT, CUSTOM_IMPORT, GRID_CELL)
GEONAF_ZONE = "zone d'acquisition GEONAF"
COMMUNITY = "référentiel communautaire"
HUMANITARIAN = "référentiel humanitaire (COD)"
USER_PROVIDED = "fourni par l'utilisateur"
GEONAF_GRID = "grille GEONAF"
OFFICIAL_VERIFIED = "officiel vérifié"
LEGAL_STATUSES = {  # Statuses allowed for each kind; "officiel vérifié" always requires evidence.
    ACQUISITION_ZONE: (GEONAF_ZONE,),
    ADMIN_UNIT: (COMMUNITY, HUMANITARIAN, OFFICIAL_VERIFIED),
    CUSTOM_IMPORT: (USER_PROVIDED, OFFICIAL_VERIFIED),
    GRID_CELL: (GEONAF_GRID,),
}
# Words reserved to administrative units: an acquisition zone or grid cell label must not suggest one.
ADMINISTRATIVE_WORDS = ("commune", "département", "departement", "arrondissement", "province", "district",
                        "canton", "préfecture", "region", "région", "municipalit")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
_ISO3 = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class TerritorySource:
    source_id: str                    # e.g. "geoboundaries-gbopen", "geonaf", "user-import".
    product: str
    version: str                      # Release / build date as published by the source.
    license_spdx: str                 # SPDX identifier or "LicenseRef-…".
    attribution: str
    url: str | None = None
    share_alike: bool = False
    reference_year: int | None = None # Year the limits describe (vintage), when stated by the source.
    retrieved_at_utc: str | None = None


@dataclass(frozen=True)
class AdminInfo:
    country_iso3: str
    level_code: str                   # Source level code, e.g. "ADM2".
    local_type: str                   # As declared by the source (« département »); never inferred.
    source_unit_id: str | None = None # e.g. geoBoundaries shapeID.
    parent_territory_id: str | None = None


@dataclass(frozen=True)
class GridInfo:
    grid_system: str                  # e.g. "geonaf-wgs84-0.05deg".
    cell_id: str
    cell_size_deg: float


@dataclass(frozen=True)
class TerritoryRecord:
    territory_id: str
    version: int
    label: str
    kind: str
    legal_status: str
    source: TerritorySource
    geometry_sha256: str
    file_sha256: str
    storage_key: str                  # Relative to the registry root.
    wgs84_bounds: tuple[float, float, float, float]
    area_km2: float
    vertices: int
    parts: int
    created_at_utc: str
    admin: AdminInfo | None = None
    grid: GridInfo | None = None
    legal_evidence: str | None = None # Reference to an official act or document; required for "officiel vérifié".
    scientific_profile_id: str | None = None
    limits: tuple[str, ...] = ()
    supersedes: str | None = None     # Fingerprint of the previous version.
    schema: str = SCHEMA

    def __post_init__(self):
        validate_record(self)

    @property
    def is_official(self) -> bool:
        return self.legal_status == OFFICIAL_VERIFIED

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(record_to_dict(self)).encode("utf-8")).hexdigest()


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} obligatoire.")


def validate_record(record: TerritoryRecord) -> None:
    """Consistency rules between kind, legal status, administrative and grid information."""
    if not _ID.fullmatch(record.territory_id or ""):
        raise ValueError("Identifiant de territoire invalide (minuscules, chiffres, « - » et « _ »).")
    if not isinstance(record.version, int) or record.version < 1:
        raise ValueError("Version de territoire invalide.")
    if record.kind not in KINDS:
        raise ValueError("Type de territoire inconnu.")
    _text(record.label, "Libellé")
    if record.legal_status not in LEGAL_STATUSES[record.kind]:
        raise ValueError(f"Statut « {record.legal_status} » interdit pour le type {record.kind}.")
    if record.is_official and (not isinstance(record.legal_evidence, str) or len(record.legal_evidence.strip()) < 8):
        raise ValueError("Une limite officielle exige un justificatif vérifiable (acte, référence, document).")
    source = record.source
    for name in ("source_id", "product", "version", "license_spdx", "attribution"):
        _text(getattr(source, name), f"Source : {name}")
    if record.kind in (ACQUISITION_ZONE, GRID_CELL):
        if record.admin is not None:
            raise ValueError("Une zone d'acquisition ou une maille de grille n'est pas une unité administrative.")
        if any(word in record.label.lower() for word in ADMINISTRATIVE_WORDS):
            raise ValueError("Le libellé d'une zone d'acquisition ou d'une maille ne doit pas évoquer une "
                             "unité administrative.")
    if record.kind == ADMIN_UNIT and record.admin is None:
        raise ValueError("Une unité administrative doit décrire son niveau et son type local.")
    if record.admin is not None:
        if not _ISO3.fullmatch(record.admin.country_iso3 or ""):
            raise ValueError("Code pays ISO 3166-1 alpha-3 attendu.")
        _text(record.admin.level_code, "Niveau administratif")
        _text(record.admin.local_type, "Type local déclaré par la source")
    if (record.kind == GRID_CELL) != (record.grid is not None):
        raise ValueError("Une maille de grille, et elle seule, décrit son système de grille.")
    if not (len(record.geometry_sha256) == len(record.file_sha256) == 64):
        raise ValueError("Empreintes SHA-256 attendues.")
    if not record.storage_key.startswith("geometries/"):
        raise ValueError("Clé de géométrie hors du registre.")


def record_to_dict(record: TerritoryRecord) -> dict:
    payload = asdict(record)
    payload["wgs84_bounds"] = list(record.wgs84_bounds)
    payload["limits"] = list(record.limits)
    return payload


def record_from_dict(payload: dict) -> TerritoryRecord:
    if payload.get("schema") != SCHEMA:
        raise ValueError("Schéma d'enregistrement de territoire inconnu.")
    data = dict(payload)
    data["source"] = TerritorySource(**data["source"])
    data["admin"] = AdminInfo(**data["admin"]) if data.get("admin") else None
    data["grid"] = GridInfo(**data["grid"]) if data.get("grid") else None
    data["wgs84_bounds"] = tuple(data["wgs84_bounds"])
    data["limits"] = tuple(data.get("limits") or ())
    return TerritoryRecord(**data)


_METADATA = ("label", "kind", "legal_status", "source", "admin", "grid", "legal_evidence", "scientific_profile_id",
             "limits")


@dataclass
class TerritoryRegistry:
    """File implementation; a PostGIS repository can replace it with the same methods."""

    data_dir: Path
    store: LayerFileStore = field(init=False)

    def __post_init__(self):
        self.store = LayerFileStore(Path(self.data_dir) / "territories")

    def versions(self, territory_id: str) -> tuple[TerritoryRecord, ...]:
        if not _ID.fullmatch(territory_id or ""):
            raise ValueError("Identifiant de territoire invalide.")
        records = []
        for key in self.store.keys(f"records/{territory_id}"):
            if key.endswith(".json"):
                payload = json.loads(self.store.path(key).read_text(encoding="utf-8"))
                record = record_from_dict(payload["record"])
                if payload.get("fingerprint") != record.fingerprint or record.territory_id != territory_id:
                    raise ValueError("Enregistrement de territoire modifié depuis son écriture.")
                records.append(record)
        return tuple(sorted(records, key=lambda r: r.version))

    def latest(self, territory_id: str) -> TerritoryRecord | None:
        versions = self.versions(territory_id)
        return versions[-1] if versions else None

    def get(self, territory_id: str, version: int | None = None) -> TerritoryRecord:
        versions = self.versions(territory_id)
        match = versions[-1:] if version is None else [r for r in versions if r.version == version]
        if not match:
            raise KeyError(f"Territoire inconnu : {territory_id}" + ("" if version is None else f" v{version}"))
        return match[0]

    def territories(self, kind: str | None = None) -> tuple[TerritoryRecord, ...]:
        """Latest version of every territory, optionally of one kind."""
        ids = sorted({key.split("/")[1] for key in self.store.keys("records") if key.endswith(".json")})
        latest = (self.latest(territory_id) for territory_id in ids)
        return tuple(r for r in latest if r is not None and (kind is None or r.kind == kind))

    def find_geometry(self, geometry_sha256: str) -> tuple[TerritoryRecord, ...]:
        """Every territory (latest versions) registered with this exact geometry."""
        return tuple(r for r in self.territories() if r.geometry_sha256 == geometry_sha256)

    def geometry_path(self, record: TerritoryRecord) -> Path:
        """Path of the registered geometry, verified against the record before use."""
        path = self.store.path(record.storage_key)
        if not path.is_file() or sha256_file(path) != record.file_sha256:
            raise ValueError("Géométrie de territoire absente ou modifiée depuis son enregistrement.")
        return path

    def register(self, *, territory_id: str, label: str, kind: str, legal_status: str, source: TerritorySource,
                 geometry_path, admin: AdminInfo | None = None, grid: GridInfo | None = None,
                 legal_evidence: str | None = None, scientific_profile_id: str | None = None,
                 limits=(), now: datetime | None = None) -> TerritoryRecord:
        """Register a territory or a new version of it; identical content returns the existing version.

        The geometry is validated strictly (no repair). A different geometry or
        different metadata creates version n+1 that ``supersedes`` version n;
        nothing is ever overwritten.
        """
        from src.geo.change_analysis import load_equal_area_aoi

        geometry_path = Path(geometry_path)
        identity = load_aoi_identity(geometry_path, territory_id)
        area_km2 = load_equal_area_aoi(geometry_path).area_m2 / 1e6
        key = f"geometries/{identity.geometry_sha256}.geojson"
        file_sha = identity.file_sha256
        if self.store.exists(key):
            file_sha = sha256_file(self.store.path(key))   # First file registered for this geometry is kept.
            if load_aoi_identity(self.store.path(key)).geometry_sha256 != identity.geometry_sha256:
                raise ValueError("Géométrie stockée incohérente avec son empreinte.")
        previous = self.latest(territory_id)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidate = TerritoryRecord(
            territory_id, (previous.version + 1) if previous else 1, label, kind, legal_status, source,
            identity.geometry_sha256, file_sha, key, identity.wgs84_bounds, area_km2, identity.vertices,
            identity.parts, now.isoformat(), admin, grid, legal_evidence, scientific_profile_id, tuple(limits),
            previous.fingerprint if previous else None)
        if previous is not None and previous.geometry_sha256 == candidate.geometry_sha256 and all(
                getattr(previous, name) == getattr(candidate, name) for name in _METADATA):
            return previous
        if not self.store.exists(key):
            self.store.write_bytes(key, geometry_path.read_bytes())
        payload = {"record": record_to_dict(candidate), "fingerprint": candidate.fingerprint}
        self.store.write_bytes(f"records/{territory_id}/v{candidate.version:04d}.json",
                               _canonical(payload).encode("utf-8"))
        return candidate


HISTORICAL_ZONES = {"akanda": "Akanda", "libreville": "Libreville", "owendo": "Owendo"}
HISTORICAL_LIMITS = (
    "Carré d'acquisition de 12 × 12 km centré sur une localité ; ce n'est pas une limite administrative.",
    "Les zones d'acquisition voisines se recouvrent.",
)


def register_historical_zones(registry: TerritoryRegistry, aoi_dir, *, now: datetime | None = None):
    """Register the historical 12 km squares as acquisition zones, keeping their identifiers.

    The registry copy is byte-identical to ``data/aoi/<id>.geojson``: the
    geometry fingerprint, the file digest and every cache key stay unchanged.
    """
    from src.geo.scientific_profile import REFERENCE_PROFILES

    output = []
    for aoi_id, name in HISTORICAL_ZONES.items():
        path = Path(aoi_dir) / f"{aoi_id}.geojson"
        if not path.is_file():
            continue
        profile = REFERENCE_PROFILES.get(aoi_id)
        output.append(registry.register(
            territory_id=aoi_id, label=f"Zone d'acquisition {name} (12 × 12 km)", kind=ACQUISITION_ZONE,
            legal_status=GEONAF_ZONE,
            source=TerritorySource("geonaf", "Zones d'acquisition GEONAF", "2026.1", "LicenseRef-GEONAF",
                                   "GEONAF"),
            geometry_path=path, scientific_profile_id=profile.profile_id if profile else None,
            limits=HISTORICAL_LIMITS, now=now))
    return tuple(output)
