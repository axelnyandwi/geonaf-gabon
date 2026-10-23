"""geoBoundaries adapter: bounded retrieval of administrative referentials into the territory registry.

geoBoundaries publishes community (``gbOpen``) and humanitarian
(``gbHumanitarian``, from OCHA COD-AB) boundaries. Neither is an official
publication of the country: territories are registered as *référentiel
communautaire* or *référentiel humanitaire (COD)*, never *officiel vérifié*.

* Metadata are requested per country and level (a few kB). Only the levels
  the source publishes exist: nothing is invented (Gabon: ADM0–ADM2, no
  communes).
* The GeoJSON is downloaded only if no stored snapshot of the same release,
  country, level and build exists. Every request goes through
  ``ExternalHttpClient`` (GEONAF TLS policy, size ceilings, retries); redirects
  are followed by hand, at most three, each hop re-checked against the host
  allow-list.
* The raw response is stored once, never overwritten, with its metadata, the
  retrieval date and its SHA-256.
* Licences are mapped to SPDX identifiers from an explicit table; an unknown
  or non-commercial licence stops the import.
* Each unit is validated alone (``src.geo.aoi_identity``): invalid geometries
  are rejected and counted, never repaired. Valid units are registered with
  identifiers that contain the release, so two sources never share a
  territory; a new build of the same unit becomes a new version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import urljoin

from src.data.external_http import HostPolicy
from src.data.territory_registry import (
    ADMIN_UNIT, COMMUNITY, HUMANITARIAN, AdminInfo, TerritoryRegistry, TerritorySource,
)

API_URL = "https://www.geoboundaries.org/api/current/{release}/{iso3}/{level}/"
RELEASES = {"gbOpen": COMMUNITY, "gbHumanitarian": HUMANITARIAN}
LEVELS = ("ADM0", "ADM1", "ADM2", "ADM3", "ADM4", "ADM5")
MAX_METADATA_BYTES = 256 * 1024
MAX_GEOJSON_BYTES = 32 * 1024 * 1024
MAX_REDIRECTS = 3
HOSTS = {
    "www.geoboundaries.org": HostPolicy("geoboundaries", 1.0, MAX_METADATA_BYTES),
    "github.com": HostPolicy("github", 1.0, 64 * 1024),                 # Redirects only.
    "raw.githubusercontent.com": HostPolicy("github-raw", 1.0, MAX_GEOJSON_BYTES),
    "media.githubusercontent.com": HostPolicy("github-lfs", 1.0, MAX_GEOJSON_BYTES),
}
# Licence strings published by geoBoundaries → (SPDX identifier, share-alike). Anything else is refused.
LICENSES = {
    "creative commons attribution 3.0 license": ("CC-BY-3.0", False),
    "creative commons attribution 3.0 unported": ("CC-BY-3.0", False),
    "creative commons attribution 4.0 international (cc by 4.0)": ("CC-BY-4.0", False),
    "creative commons attribution 4.0 international": ("CC-BY-4.0", False),
    "creative commons attribution-sharealike 3.0 unported": ("CC-BY-SA-3.0", True),
    "creative commons attribution-sharealike 4.0 international": ("CC-BY-SA-4.0", True),
    "creative commons attribution 3.0 intergovernmental organisations (cc by 3.0 igo)": ("CC-BY-3.0-IGO", False),
    "open data commons open database license 1.0": ("ODbL-1.0", True),
    "public domain": ("LicenseRef-PublicDomain", False),
}
REQUIRED_METADATA = ("boundaryISO", "boundaryType", "boundaryName", "boundaryLicense", "boundarySource",
                     "buildDate", "gjDownloadURL")
REDIRECT_CODES = (301, 302, 303, 307, 308)


class GeoBoundariesError(ValueError):
    """Import refused or failed; the message never contains a local path."""


@dataclass(frozen=True)
class BoundarySnapshot:
    release: str
    iso3: str
    level: str
    metadata: dict
    storage_key: str              # Raw GeoJSON in the external layer store.
    path: Path
    sha256: str
    retrieved_at_utc: str
    reused: bool
    network_bytes: int


@dataclass(frozen=True)
class ImportReport:
    snapshot: BoundarySnapshot
    features: int
    declared_units: int | None
    registered: tuple             # TerritoryRecord, in source order.
    rejected: tuple[tuple[str, str], ...]   # (unit name, reason)
    warnings: tuple[str, ...]


def _check_release_level(release, iso3, level):
    if release not in RELEASES:
        raise GeoBoundariesError("Jeu geoBoundaries non pris en charge (gbOpen ou gbHumanitarian).")
    if not re.fullmatch(r"[A-Z]{3}", iso3 or ""):
        raise GeoBoundariesError("Code pays ISO 3166-1 alpha-3 attendu.")
    if level not in LEVELS:
        raise GeoBoundariesError("Niveau administratif inconnu.")


def get_bounded(client, url: str, max_bytes: int) -> bytes:
    """GET with manual redirects; ``client.request`` re-checks the host of every hop."""
    for _ in range(MAX_REDIRECTS + 1):
        try:
            client.policy(url)
        except ValueError:
            host = url.split("/")[2] if url.count("/") >= 2 else "?"
            raise GeoBoundariesError(f"Hôte non autorisé ou URL non HTTPS refusé : {host}.") from None
        response = client.request("GET", url, max_bytes=max_bytes, expected=(200,) + REDIRECT_CODES)
        if response.status_code == 200:
            return response.content
        location = {k.lower(): v for k, v in response.headers.items()}.get("location")
        if not location:
            raise GeoBoundariesError("Redirection sans destination.")
        url = urljoin(url, location)
        if not url.startswith("https://"):
            raise GeoBoundariesError("Redirection vers une URL non HTTPS refusée.")
    raise GeoBoundariesError("Trop de redirections.")


def license_of(metadata: dict) -> tuple[str, bool]:
    text = " ".join(str(metadata.get("boundaryLicense", "")).split()).lower()
    if "noncommercial" in text.replace("-", "").replace(" ", "") or "(cc by-nc" in text:
        raise GeoBoundariesError("Licence non commerciale : import refusé.")
    if text not in LICENSES:
        raise GeoBoundariesError(f"Licence non reconnue (« {metadata.get('boundaryLicense')} ») : import refusé.")
    return LICENSES[text]


def build_date(metadata: dict) -> str:
    raw = str(metadata.get("buildDate", "")).strip()
    for pattern in ("%b %d, %Y", "%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    raise GeoBoundariesError("Date de production geoBoundaries illisible.")


def reference_year(metadata: dict) -> int | None:
    value = str(metadata.get("boundaryYearRepresented", "")).strip()
    return int(value) if re.fullmatch(r"(19|20)\d\d", value) else None


def fetch_metadata(client, *, release: str, iso3: str, level: str) -> dict:
    _check_release_level(release, iso3, level)
    try:
        raw = get_bounded(client, API_URL.format(release=release, iso3=iso3, level=level), MAX_METADATA_BYTES)
    except GeoBoundariesError:
        raise
    except Exception as error:
        status = getattr(error, "status_code", None)
        if status == 404:
            raise GeoBoundariesError(f"{release} ne publie pas le niveau {level} pour {iso3}.") from None
        raise GeoBoundariesError(f"Métadonnées geoBoundaries inaccessibles ({type(error).__name__}).") from None
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GeoBoundariesError("Réponse de métadonnées invalide (JSON attendu).") from None
    if isinstance(metadata, list) and len(metadata) == 1:
        metadata = metadata[0]
    if not isinstance(metadata, dict) or any(not metadata.get(key) for key in REQUIRED_METADATA):
        raise GeoBoundariesError("Métadonnées geoBoundaries incomplètes.")
    if metadata["boundaryISO"] != iso3 or metadata["boundaryType"] != level:
        raise GeoBoundariesError("Métadonnées incohérentes avec le pays ou le niveau demandé.")
    if not str(metadata["gjDownloadURL"]).startswith("https://"):
        raise GeoBoundariesError("URL de téléchargement non HTTPS.")
    license_of(metadata)
    build_date(metadata)
    return metadata


def _prefix(release, iso3, level):
    return f"geoboundaries/{release}/{iso3}/{level}"


def fetch_boundaries(client, store, *, release: str, iso3: str, level: str,
                     now: datetime | None = None) -> BoundarySnapshot:
    """Stored snapshot for the current build, downloading the GeoJSON only when none exists."""
    metadata = fetch_metadata(client, release=release, iso3=iso3, level=level)
    build = build_date(metadata)
    prefix = _prefix(release, iso3, level)
    for key in store.keys(prefix):
        if key.endswith(".meta.json") and key.rsplit("/", 1)[1].startswith(f"{build}-"):
            stored = json.loads(store.path(key).read_text(encoding="utf-8"))
            if stored["metadata"].get("gjDownloadURL") == metadata["gjDownloadURL"]:
                data_key = key.removesuffix(".meta.json") + ".geojson"
                path = store.path(data_key)
                if hashlib.sha256(path.read_bytes()).hexdigest() != stored["sha256"]:
                    raise GeoBoundariesError("Instantané geoBoundaries local modifié depuis son enregistrement.")
                return BoundarySnapshot(release, iso3, level, stored["metadata"], data_key, path, stored["sha256"],
                                        stored["retrieved_at_utc"], True, 0)
    try:
        content = get_bounded(client, metadata["gjDownloadURL"], MAX_GEOJSON_BYTES)
    except GeoBoundariesError:
        raise
    except Exception as error:
        raise GeoBoundariesError(f"Téléchargement geoBoundaries impossible ({type(error).__name__}).") from None
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GeoBoundariesError("Réponse GeoJSON invalide.") from None
    if payload.get("type") != "FeatureCollection" or not payload.get("features"):
        raise GeoBoundariesError("GeoJSON sans entité.")
    digest = hashlib.sha256(content).hexdigest()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    base = f"{prefix}/{build}-{digest[:16]}"
    path = store.write_bytes(f"{base}.geojson", content)
    store.write_bytes(f"{base}.meta.json", json.dumps(
        {"metadata": metadata, "sha256": digest, "retrieved_at_utc": now.isoformat(),
         "metadata_url": API_URL.format(release=release, iso3=iso3, level=level)},
        ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return BoundarySnapshot(release, iso3, level, metadata, f"{base}.geojson", path, digest, now.isoformat(), False,
                            len(content))


def slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def territory_id(release: str, iso3: str, level: str, name: str, unit_id: str, duplicate: bool) -> str:
    """Stable across builds for the same release and name; the release is part of the identifier."""
    base = f"{iso3.lower()}-{release.lower()}-{level.lower()}-"
    suffix = f"-{hashlib.sha256(unit_id.encode()).hexdigest()[:6]}" if duplicate else ""
    return base + (slug(name)[: 64 - len(base) - len(suffix)] or "sans-nom") + suffix


def _local_type(metadata, level):
    canonical = " ".join(str(metadata.get("boundaryCanonical") or "").split())
    if not canonical or canonical.lower() in ("unknown", "nan", "none"):
        return f"non déclaré par la source ({level})"
    return canonical   # Verbatim, never translated or inferred.


def register_snapshot(registry: TerritoryRegistry, snapshot: BoundarySnapshot, *,
                      now: datetime | None = None) -> ImportReport:
    """Validate each unit alone and register the valid ones; invalid units are reported, never repaired."""
    import geopandas as gpd

    from src.geo.aoi_identity import validate_polygonal_frame

    metadata = snapshot.metadata
    spdx, share_alike = license_of(metadata)
    year = reference_year(metadata)
    frame = gpd.read_file(BytesIO(snapshot.path.read_bytes()))
    if frame.crs is None:
        raise GeoBoundariesError("GeoJSON sans système de coordonnées.")
    names = [" ".join(str(n).split()) if n is not None else "" for n in frame.get("shapeName", [None] * len(frame))]
    unit_ids = [str(u) for u in frame.get("shapeID", range(len(frame)))]
    counts = {}
    for name in names:
        counts[slug(name)] = counts.get(slug(name), 0) + 1
    source = TerritorySource(
        f"geoboundaries-{snapshot.release.lower()}", f"geoBoundaries {snapshot.release} {snapshot.iso3} "
        f"{snapshot.level}", build_date(metadata), spdx,
        f"geoBoundaries ({snapshot.release}) ; source : {metadata.get('boundarySource')}",
        metadata.get("gjDownloadURL"), share_alike, year, snapshot.retrieved_at_utc)
    status = RELEASES[snapshot.release]
    limits = (
        f"{'Référentiel communautaire' if status == COMMUNITY else 'Référentiel humanitaire (COD)'} : "
        "limites non officielles, sans valeur juridique.",
        f"Limites représentant l'année {year if year else 'non précisée'} selon la source ; "
        f"production geoBoundaries du {build_date(metadata)}.",
        "Précision géométrique limitée (contours simplifiés) ; d'autres référentiels peuvent différer fortement.",
    )
    declared = metadata.get("admUnitCount")
    declared = int(declared) if str(declared).isdigit() else None
    warnings = []
    if declared is not None and declared != len(frame):
        warnings.append(f"La source annonce {declared} unités, le fichier en contient {len(frame)}.")
    registered, rejected = [], []
    with tempfile.TemporaryDirectory() as folder:
        for index, (name, unit_id) in enumerate(zip(names, unit_ids)):
            unit = frame.iloc[[index]][["geometry"]]
            label = name or f"Unité {unit_id}"
            try:
                validate_polygonal_frame(unit)
            except ValueError as error:
                rejected.append((label, str(error)))
                continue
            path = Path(folder) / f"unit-{index}.geojson"
            unit.to_crs("EPSG:4326").to_file(path, driver="GeoJSON")
            registered.append(registry.register(
                territory_id=territory_id(snapshot.release, snapshot.iso3, snapshot.level, label, unit_id,
                                          counts[slug(name)] > 1),
                label=label, kind=ADMIN_UNIT, legal_status=status, source=source, geometry_path=path,
                admin=AdminInfo(snapshot.iso3, snapshot.level, _local_type(metadata, snapshot.level), unit_id),
                limits=limits, now=now))
    return ImportReport(snapshot, len(frame), declared, tuple(registered), tuple(rejected), tuple(warnings))


def import_geoboundaries(data_dir, *, release: str, iso3: str, level: str, client=None,
                         now: datetime | None = None) -> ImportReport:
    """Retrieve (or reuse) one release/country/level and register its units; explicit user action only."""
    from src.data.external_http import EXTERNAL_HOSTS, ExternalHttpClient
    from src.services.external_layers import open_store

    _check_release_level(release, iso3, level)
    client = client or ExternalHttpClient(hosts={**EXTERNAL_HOSTS, **HOSTS})
    store, _ = open_store(Path(data_dir))
    snapshot = fetch_boundaries(client, store, release=release, iso3=iso3, level=level, now=now)
    return register_snapshot(TerritoryRegistry(Path(data_dir)), snapshot, now=now)
