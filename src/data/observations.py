"""Acquisitions and indicator results as files (D3): reading, fingerprints, write-once storage; no network.

Both families are what migration 0002 copies into PostGIS (``src.data.postgis_observations``). The
file side stays the reference, exactly like the territory registry:

* **Acquisitions.** Each ingested product has a strict manifest
  ``raw/sentinel2/<product>/acquisition_manifest.json`` (``src.data.acquisition_manifest``). It is
  validated, fingerprinted (SHA-256 of its canonical JSON) and described by the dates it carries:
  ``acquired_at`` (sensing) and ``ingested_at`` (retrieval by GEONAF), never merged. The *footprint* is
  the extent of the red band's raster grid reprojected to WGS84 (densified edges): it is **not** the
  footprint of valid pixels. A manifest that fails validation is listed as rejected, never repaired.
* **Indicator results.** A :class:`~src.indicators.model.TerritorialIndicator` is serialized to a
  canonical document. Its *definition* (identifier, label, unit, method, nature, scientific status,
  profile fingerprint) and the *result* get separate SHA-256 fingerprints. A result is attached to the
  normalized geometry it was computed on: :meth:`IndicatorResultStore.save` reads the zone file, checks
  that its bytes are the ones the indicator names (``aoi_sha256``) and records the geometry fingerprint.
  The result fingerprint excludes the computation date: recomputing the same value on the same geometry
  is not a new result (no duplicates), and the first record is kept.

Storage below ``data/indicators/`` is write-once (``LayerFileStore``): ``results/<result_sha256>.json``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from src.data.layer_store import LayerFileStore

RESULT_SCHEMA = "geonaf.indicator-result/v1"
DEFINITION_FIELDS = ("indicator_id", "label", "unit", "method", "result_kind", "scientific_status",
                     "profile_fingerprint")
MAX_MANIFEST_BYTES = 2 * 1024 ** 2
_SHA = re.compile(r"[0-9a-f]{64}")
_FOOTPRINT_STEPS = 16                            # Points per edge when the grid extent is reprojected.


def canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# --- Acquisitions ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AcquisitionRecord:
    collection: str
    product_id: str
    acquired_at_utc: datetime
    ingested_at_utc: datetime
    manifest_sha256: str
    stac_source: str
    grid_crs: str
    grid_resolution_m: float
    footprint_wkb: bytes | None                   # WGS84 Polygon (raster grid extent), little-endian WKB.
    integrity_mode: str | None
    document: dict

    @property
    def identity(self) -> tuple[str, str]:
        return self.collection, self.product_id

    def footprint(self):
        import shapely

        return None if self.footprint_wkb is None else shapely.from_wkb(self.footprint_wkb)


def grid_footprint(crs: str, extent) -> bytes | None:
    """WGS84 polygon of a raster extent ``(left, bottom, right, top)`` in ``crs``; None if not reprojectable."""
    import shapely
    from pyproj import CRS, Transformer
    from pyproj.exceptions import ProjError
    from shapely.geometry import Polygon

    try:
        left, bottom, right, top = (float(v) for v in extent)
        transformer = Transformer.from_crs(CRS.from_user_input(crs), CRS.from_epsg(4326), always_xy=True)
        ring = []
        corners = ((left, bottom), (right, bottom), (right, top), (left, top), (left, bottom))
        for (x0, y0), (x1, y1) in zip(corners, corners[1:]):
            for step in range(_FOOTPRINT_STEPS):
                fraction = step / _FOOTPRINT_STEPS
                ring.append(transformer.transform(x0 + (x1 - x0) * fraction, y0 + (y1 - y0) * fraction))
        if not all(math.isfinite(c) for point in ring for c in point):
            return None
        polygon = Polygon(ring)
    except (ProjError, ValueError, TypeError):
        return None
    if not polygon.is_valid or polygon.is_empty:
        return None
    return shapely.to_wkb(polygon, byte_order=1, include_srid=False)


def acquisition_record(manifest: dict) -> AcquisitionRecord:
    """Validated record of a strict acquisition manifest; raises ``ValueError`` on any invalid field."""
    from src.data.acquisition_manifest import validate_acquisition_manifest

    validate_acquisition_manifest(manifest)
    document = json.loads(canonical_json(manifest))          # JSON round trip: what jsonb will hold.
    red = manifest["assets"]["b04"]
    acquired, ingested = _utc(manifest["acquired_at_utc"]), _utc(manifest["ingested_at_utc"])
    if ingested < acquired:
        raise ValueError("Date d'ingestion antérieure à la date d'acquisition.")
    integrity = manifest.get("integrity")
    return AcquisitionRecord(
        collection=manifest["collection"], product_id=manifest["product_id"], acquired_at_utc=acquired,
        ingested_at_utc=ingested, manifest_sha256=sha256_text(canonical_json(document)),
        stac_source=manifest["stac"]["source"], grid_crs=red["crs"], grid_resolution_m=float(red["resolution"][0]),
        footprint_wkb=grid_footprint(red["crs"], red["extent"]),
        integrity_mode=integrity.get("mode") if isinstance(integrity, dict) else None, document=document)


@dataclass(frozen=True)
class AcquisitionScan:
    records: tuple[AcquisitionRecord, ...]        # Ordered by acquisition date, then product.
    rejected: tuple[str, ...]                     # Folder names whose manifest was refused (no path, no detail).

    def latest(self) -> tuple[AcquisitionRecord, ...]:
        """Most recent ingestion of every product (same rule as the view geonaf.acquisitions_latest)."""
        best: dict[tuple, AcquisitionRecord] = {}
        for record in self.records:
            current = best.get(record.identity)
            if current is None or (record.ingested_at_utc, record.manifest_sha256) > (
                    current.ingested_at_utc, current.manifest_sha256):
                best[record.identity] = record
        return tuple(sorted(best.values(), key=lambda r: (r.acquired_at_utc, r.product_id)))


class FileAcquisitionCatalog:
    """Strict manifests below ``<data_dir>/raw/sentinel2``; read only."""

    def __init__(self, data_dir):
        self.root = Path(data_dir) / "raw" / "sentinel2"

    def scan(self) -> AcquisitionScan:
        records, rejected = [], []
        if not self.root.is_dir():
            return AcquisitionScan((), ())
        root = self.root.resolve()
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            path = folder / "acquisition_manifest.json"
            if not path.is_file():
                continue
            try:
                if not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_MANIFEST_BYTES:
                    raise ValueError
                records.append(acquisition_record(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, KeyError, TypeError, OSError):
                rejected.append(folder.name)
        records.sort(key=lambda r: (r.acquired_at_utc, r.product_id, r.ingested_at_utc))
        return AcquisitionScan(tuple(records), tuple(rejected))

    def latest(self) -> tuple[AcquisitionRecord, ...]:
        return self.scan().latest()

    def intersecting(self, wgs84_bounds) -> tuple[AcquisitionRecord, ...]:
        """Latest ingestions whose grid footprint intersects a WGS84 rectangle."""
        from shapely.geometry import box

        rectangle = box(*(float(v) for v in wgs84_bounds))
        return tuple(r for r in self.latest() if r.footprint_wkb is not None and r.footprint().intersects(rectangle))


# --- Indicators -----------------------------------------------------------------------------------


def _jsonable(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Date d'indicateur sans fuseau horaire.")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Valeur d'indicateur non finie.")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def indicator_document(indicator) -> dict:
    """JSON document of a TerritorialIndicator (dates in ISO 8601 UTC, tuples as lists)."""
    return _jsonable(asdict(indicator))


def _date(text):
    return None if text is None else _utc(text)


def indicator_from_document(document: dict):
    """Inverse of :func:`indicator_document`; ``indicator_document(indicator_from_document(d)) == d``."""
    from src.indicators.model import BreakdownItem, IndicatorDates, IndicatorSource, TerritorialIndicator

    data = dict(document)
    names = {f.name for f in fields(TerritorialIndicator)}
    if set(data) - names:
        raise ValueError("Champs d'indicateur inconnus : " + ", ".join(sorted(set(data) - names)))
    dates = data.get("dates")
    if dates is not None:
        data["dates"] = IndicatorDates(
            _date(dates["observation_start_utc"]), _date(dates["observation_end_utc"]), dates["observation_note"],
            dates["reference_label"], _date(dates["reference_at_utc"]), _date(dates["ingested_at_utc"]))
    if data.get("source") is not None:
        data["source"] = IndicatorSource(**data["source"])
    data["breakdown"] = tuple(BreakdownItem(**{**item, "detail": tuple(tuple(pair) for pair in item["detail"])})
                              for item in data.get("breakdown", ()))
    data["shares"] = tuple(tuple(pair) for pair in data.get("shares", ()))
    data["reliability_reasons"] = tuple(data.get("reliability_reasons", ()))
    data["limits"] = tuple(data.get("limits", ()))
    return TerritorialIndicator(**data)


def definition_document(indicator) -> dict:
    return {name: getattr(indicator, name) for name in DEFINITION_FIELDS}


def definition_sha256(indicator) -> str:
    return sha256_text(canonical_json(definition_document(indicator)))


def result_sha256(document: dict, geometry_sha256: str) -> str:
    """Identity of a result: the indicator document and the geometry; never the computation date."""
    return sha256_text(canonical_json({"indicator": document, "geometry_sha256": geometry_sha256}))


@dataclass(frozen=True)
class IndicatorResult:
    result_sha256: str
    definition_sha256: str
    geometry_sha256: str
    computed_at_utc: str
    code_version: str
    indicator: object                           # TerritorialIndicator
    document: dict                              # Envelope as stored (schema, indicator, ids, dates).


def result_envelope(indicator, *, geometry_sha256: str, computed_at_utc: str, code_version: str) -> dict:
    document = indicator_document(indicator)
    return {"schema": RESULT_SCHEMA, "indicator": document, "definition": definition_document(indicator),
            "definition_sha256": definition_sha256(indicator), "geometry_sha256": geometry_sha256,
            "computed_at_utc": computed_at_utc, "code_version": code_version}


def result_from_envelope(envelope: dict, expected_sha256: str | None = None) -> IndicatorResult:
    """Rebuild and verify a stored result: schema, definition and result fingerprints."""
    if envelope.get("schema") != RESULT_SCHEMA:
        raise ValueError("Schéma de résultat d'indicateur inconnu.")
    geometry = envelope.get("geometry_sha256")
    if not isinstance(geometry, str) or not _SHA.fullmatch(geometry):
        raise ValueError("Empreinte de géométrie invalide dans le résultat.")
    indicator = indicator_from_document(envelope["indicator"])
    if indicator_document(indicator) != envelope["indicator"]:
        raise ValueError("Document d'indicateur non canonique.")
    if envelope.get("definition") != definition_document(indicator) or \
            envelope.get("definition_sha256") != definition_sha256(indicator):
        raise ValueError("Définition d'indicateur incohérente avec le résultat.")
    sha = result_sha256(envelope["indicator"], geometry)
    if expected_sha256 is not None and sha != expected_sha256:
        raise ValueError("Résultat d'indicateur modifié depuis son enregistrement.")
    _utc(envelope["computed_at_utc"])
    if not isinstance(envelope.get("code_version"), str) or not envelope["code_version"].strip():
        raise ValueError("Version du code absente du résultat.")
    return IndicatorResult(sha, envelope["definition_sha256"], geometry, envelope["computed_at_utc"],
                           envelope["code_version"], indicator, envelope)


class IndicatorResultStore:
    """Write-once indicator results below ``<data_dir>/indicators``."""

    def __init__(self, data_dir):
        self.store = LayerFileStore(Path(data_dir) / "indicators")

    def save(self, indicator, zone_path, *, code_version: str, computed_at: datetime | None = None) -> IndicatorResult:
        """Record one result computed on ``zone_path``; the same result already stored is returned as is."""
        from src.demo_mode import ensure_writable
        from src.geo.aoi_identity import load_aoi_identity

        ensure_writable("Enregistrement de résultat d'indicateur")
        identity = load_aoi_identity(Path(zone_path))
        if identity.file_sha256 != indicator.aoi_sha256:
            raise ValueError("Le fichier de zone n'est pas celui sur lequel l'indicateur a été calculé.")
        computed = (computed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        envelope = result_envelope(indicator, geometry_sha256=identity.geometry_sha256, computed_at_utc=computed,
                                   code_version=code_version)
        sha = result_sha256(envelope["indicator"], identity.geometry_sha256)
        key = f"results/{sha}.json"
        if self.store.exists(key):
            return self._read(key)
        self.store.write_bytes(key, canonical_json({"result": envelope, "result_sha256": sha}).encode("utf-8"))
        return result_from_envelope(envelope, sha)

    def _read(self, key: str) -> IndicatorResult:
        payload = json.loads(self.store.path(key).read_text(encoding="utf-8"))
        expected = key.rsplit("/", 1)[-1].removesuffix(".json")
        if payload.get("result_sha256") != expected:
            raise ValueError("Nom de fichier de résultat incohérent avec son empreinte.")
        return result_from_envelope(payload["result"], expected)

    def results(self, geometry_sha256: str | None = None) -> tuple[IndicatorResult, ...]:
        """Every stored result (verified), optionally for one geometry; ordered by indicator then date."""
        output = [self._read(key) for key in self.store.keys("results") if key.endswith(".json")]
        if geometry_sha256 is not None:
            output = [r for r in output if r.geometry_sha256 == geometry_sha256]
        return tuple(sorted(output, key=lambda r: (r.indicator.indicator_id, r.computed_at_utc, r.result_sha256)))
