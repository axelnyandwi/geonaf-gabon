"""Resource guards: estimate the cost of an analysis **before** running it; no UI, no network.

Estimates are deliberately prudent (upper bounds) and every refusal states its
reason. Thresholds are **provisional**: taken from the 23/10/2026 diagnostic
and calibrated on one machine (``scripts/benchmark_resource_memory.py``), not
validated scientifically or operationally. They are configurable
(``ResourceLimits``, environment variables ``GEONAF_LIMIT_*``) and the memory
thresholds also follow the memory available on the host (warning above
half of it, refusal above 90 %).

What is estimated:

* raster cells: the UTM grid at the analysis resolution covering the zone's
  **bounding box** (what the pipelines allocate), not only the polygon area;
* memory: post-import baseline + cells × bytes per cell (pair analysis, with a
  safety factor) + OSM JSON bytes × expansion factor (parsing dominates);
* Sentinel-2 tiles: 100 km MGRS squares touched in each UTM zone/hemisphere —
  an upper bound of the tiles to read (tiles overlap by 9.8 km);
* OSM: bounding box against the bounded Overpass query, estimated volume
  (``out count``) when known — unknown is reported, never assumed small;
* GeoJSON imports: file size **before parsing**, then vertices and parts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
import os
from pathlib import Path

OK, WARNING, REFUSED, UNKNOWN = "ok", "avertissement", "refus", "inconnu"
STATUS_NOTE = "Seuils provisoires, calibrés sur un seul poste ; non validés scientifiquement ni opérationnellement."
MIB = 1024 * 1024


@dataclass(frozen=True)
class ResourceLimits:
    resolution_m: float = 10.0
    warn_cells: int = 4_000_000
    max_cells: int = 16_000_000
    warn_memory_bytes: int = 1024 * MIB
    max_memory_bytes: int = 2048 * MIB
    available_memory_fraction: float = 0.5       # Warn above this share of the host's free memory.
    available_memory_refuse_fraction: float = 0.9   # Refuse above this share (the estimate is already an upper bound).
    warn_s2_tiles: int = 1                       # Warn above.
    max_s2_tiles: int = 4
    warn_osm_bytes: int = 32 * MIB
    max_osm_bytes: int = 64 * MIB
    max_osm_bbox_degrees: float = 0.5            # Single bounded Overpass query (tiling not implemented).
    max_import_bytes: int = 5 * MIB
    max_import_vertices: int = 50_000
    max_import_parts: int = 1_000
    # Calibration (benchmark 23/10/2026, Windows, process peak above the post-import baseline):
    # NDVI one date 46–66 B/cell, pair analysis 104 B/cell ⇒ 104 × 1.5 safety.
    bytes_per_cell: float = 156.0
    baseline_bytes: int = 160 * MIB               # Measured 124 MiB after imports, rounded up.
    osm_expansion: float = 20.0                   # Measured 402 MiB for 24.3 MB of Overpass JSON (16.5×).

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Limite {item.name} invalide.")
        if not self.available_memory_fraction <= self.available_memory_refuse_fraction <= 1:
            raise ValueError("Limites available_memory_fraction / available_memory_refuse_fraction invalides.")
        for warn, refuse in (("warn_cells", "max_cells"), ("warn_memory_bytes", "max_memory_bytes"),
                             ("warn_s2_tiles", "max_s2_tiles"), ("warn_osm_bytes", "max_osm_bytes")):
            if getattr(self, warn) > getattr(self, refuse):
                raise ValueError(f"Seuil d'avertissement {warn} supérieur au seuil de refus {refuse}.")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_environment(cls, environ=None) -> "ResourceLimits":
        """Defaults overridden by ``GEONAF_LIMIT_<FIELD>`` (e.g. ``GEONAF_LIMIT_MAX_CELLS=8000000``)."""
        environ = os.environ if environ is None else environ
        overrides = {}
        for item in fields(cls):
            raw = environ.get(f"GEONAF_LIMIT_{item.name.upper()}")
            if raw is not None:
                try:
                    overrides[item.name] = type(item.default)(float(raw)) if item.default is not None else float(raw)
                except ValueError:
                    raise ValueError(f"Variable GEONAF_LIMIT_{item.name.upper()} non numérique.") from None
        return replace(cls(), **overrides)


@dataclass(frozen=True)
class Check:
    name: str
    label: str
    value: float | None
    unit: str
    warn_at: float | None
    refuse_at: float | None
    status: str
    reason: str


@dataclass(frozen=True)
class ResourceAssessment:
    checks: tuple[Check, ...]
    limits_fingerprint: str
    note: str = STATUS_NOTE

    @property
    def allowed(self) -> bool:
        return all(check.status != REFUSED for check in self.checks)

    @property
    def refusals(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status == REFUSED)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status in (WARNING, UNKNOWN))

    def get(self, name: str) -> Check:
        return next(c for c in self.checks if c.name == name)


def _threshold(name, label, value, unit, warn, refuse, what) -> Check:
    if value is None:
        return Check(name, label, None, unit, warn, refuse, UNKNOWN, f"{what} inconnu : non estimé.")
    if value > refuse:
        return Check(name, label, value, unit, warn, refuse, REFUSED,
                     f"{what} estimé {value:,.0f} {unit} > limite {refuse:,.0f} {unit}.".replace(",", " "))
    if value > warn:
        return Check(name, label, value, unit, warn, refuse, WARNING,
                     f"{what} estimé {value:,.0f} {unit} > seuil d'avertissement {warn:,.0f} {unit}.".replace(",", " "))
    return Check(name, label, value, unit, warn, refuse, OK, f"{what} dans les limites.")


# --- Geometry-based estimates ----------------------------------------------------------------------------


def _utm_parts(geometry_wgs84):
    """[(epsg, part)] of a geometry split by UTM zone (6°) and hemisphere."""
    from shapely.geometry import box

    left, bottom, right, top = geometry_wgs84.bounds
    output = []
    for zone in range(int((left + 180) // 6) + 1, int((min(right, 179.999999) + 180) // 6) + 2):
        west = -180 + 6 * (zone - 1)
        for south, (s, n) in ((False, (0, 90)), (True, (-90, 0))):
            part = geometry_wgs84.intersection(box(west, s, west + 6, n))
            if not part.is_empty and part.area > 0:
                output.append((32700 + zone if south else 32600 + zone, part))
    return output


def _project(geometry, epsg):
    import geopandas as gpd

    return gpd.GeoSeries([geometry], crs="EPSG:4326").to_crs(f"EPSG:{epsg}").iloc[0]


def estimate_raster_cells(geometry_wgs84, resolution_m: float = 10.0) -> tuple[int, int, int]:
    """(width, height, cells) of the UTM grid covering the bounding box, summed over UTM zones."""
    width = height = cells = 0
    for epsg, part in _utm_parts(geometry_wgs84):
        left, bottom, right, top = _project(part, epsg).bounds
        w, h = math.ceil((right - left) / resolution_m) + 1, math.ceil((top - bottom) / resolution_m) + 1
        width, height, cells = max(width, w), max(height, h), cells + w * h
    return width, height, cells


def estimate_s2_tiles(geometry_wgs84) -> tuple[tuple[int, int, int], ...]:
    """100 km MGRS squares touched, as (epsg, easting // 100 km, northing // 100 km); an upper bound."""
    from shapely.geometry import box

    squares = []
    for epsg, part in _utm_parts(geometry_wgs84):
        projected = _project(part, epsg)
        left, bottom, right, top = projected.bounds
        for e in range(int(left // 100_000), int(right // 100_000) + 1):
            for n in range(int(bottom // 100_000), int(top // 100_000) + 1):
                square = box(e * 1e5, n * 1e5, (e + 1) * 1e5, (n + 1) * 1e5)
                if projected.intersection(square).area > 0:
                    squares.append((epsg, e, n))
    return tuple(squares)


CGROUP_ROOT = Path("/sys/fs/cgroup")
CGROUP_FILES = (("memory.max", "memory.current", "memory.stat", "inactive_file"),                       # v2
                ("memory/memory.limit_in_bytes", "memory/memory.usage_in_bytes", "memory/memory.stat",
                 "total_inactive_file"))                                                               # v1
CGROUP_UNLIMITED = 1 << 60     # cgroup v1 writes a huge page-aligned number when no limit is set.


def cgroup_memory_bytes(root: Path | None = None) -> int | None:
    """Memory left under the container's cgroup limit (v2, else v1); None without a limit or when unreadable.

    Usage minus inactive file cache (reclaimable), as container runtimes count
    the working set; never below zero.
    """
    root = CGROUP_ROOT if root is None else root
    for limit_name, usage_name, stat_name, inactive_key in CGROUP_FILES:
        try:
            limit = (root / limit_name).read_text(encoding="ascii").strip()
            usage = int((root / usage_name).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        if not limit.isdigit() or int(limit) >= CGROUP_UNLIMITED:
            return None
        inactive = 0
        try:
            for line in (root / stat_name).read_text(encoding="ascii").splitlines():
                name, _, value = line.partition(" ")
                if name == inactive_key:
                    inactive = int(value)
        except (OSError, ValueError):
            pass
        return max(0, int(limit) - max(0, usage - inactive))
    return None


def available_memory_bytes() -> int | None:
    """Free memory: the smaller of the host's and of the container's cgroup limit, None when both are unreadable.

    In a container the host figure ignores the cgroup limit; without any figure the configured ceilings apply
    alone.
    """
    host = None
    try:
        import psutil

        host = int(psutil.virtual_memory().available)
    except (ImportError, OSError):
        pass
    figures = [value for value in (host, cgroup_memory_bytes()) if value is not None]
    return min(figures) if figures else None


# --- Assessments -------------------------------------------------------------------------------------------


def assess_zone(geometry_wgs84, *, limits: ResourceLimits | None = None, osm_estimated_bytes: int | None = None,
                osm_wanted: bool = True, available_bytes: int | None = -1) -> ResourceAssessment:
    """Checks for an analysis of one zone (raster cells, memory, S2 tiles, OSM).

    ``available_bytes=-1`` reads the host's free memory (psutil); ``None``
    means unknown and leaves the configured ceiling alone.
    """
    limits = limits or ResourceLimits.from_environment()
    if geometry_wgs84 is None or geometry_wgs84.is_empty or geometry_wgs84.area <= 0:
        raise ValueError("Zone vide : estimation impossible.")
    _, _, cells = estimate_raster_cells(geometry_wgs84, limits.resolution_m)
    checks = [_threshold("raster_cells", f"Cellules raster ({limits.resolution_m:g} m, emprise)", cells, "cellules",
                         limits.warn_cells, limits.max_cells, "Nombre de cellules")]
    osm_bytes = osm_estimated_bytes if osm_wanted else 0
    memory = None if osm_bytes is None else int(
        limits.baseline_bytes + cells * limits.bytes_per_cell + osm_bytes * limits.osm_expansion)
    available = available_memory_bytes() if available_bytes == -1 else available_bytes
    ceiling = limits.max_memory_bytes if available is None else min(
        limits.max_memory_bytes, int(available * limits.available_memory_refuse_fraction))
    warn_at = limits.warn_memory_bytes if available is None else min(
        limits.warn_memory_bytes, int(available * limits.available_memory_fraction))
    raster_only = int(limits.baseline_bytes + cells * limits.bytes_per_cell)
    if memory is None:
        check = _threshold("memory", "Mémoire estimée", raster_only / MIB, "Mio", min(warn_at, ceiling) / MIB,
                           ceiling / MIB, "Besoin en mémoire (hors OSM)")
        if check.status == OK:
            check = replace(check, status=UNKNOWN, reason="Mémoire raster dans les limites ; part OSM inconnue "
                                                          "(volume non estimé).")
    else:
        check = _threshold("memory", "Mémoire estimée", memory / MIB, "Mio", min(warn_at, ceiling) / MIB,
                           ceiling / MIB, "Besoin en mémoire")
    if ceiling < limits.max_memory_bytes or warn_at < limits.warn_memory_bytes:
        check = replace(check, reason=check.reason + f" Seuils adaptés à la mémoire libre de l'hôte "
                        f"({available / MIB:,.0f} Mio) : avertissement {warn_at / MIB:,.0f} Mio, refus "
                        f"{ceiling / MIB:,.0f} Mio.".replace(",", " "))
    checks.append(check)
    tiles = estimate_s2_tiles(geometry_wgs84)
    checks.append(_threshold("s2_tiles", "Tuiles Sentinel-2 (carrés MGRS 100 km)", len(tiles), "tuiles",
                             limits.warn_s2_tiles, limits.max_s2_tiles, "Nombre de tuiles"))
    if osm_wanted:
        left, bottom, right, top = geometry_wgs84.bounds
        span = max(right - left, top - bottom)
        if span > limits.max_osm_bbox_degrees:
            checks.append(Check("osm_bbox", "Emprise OSM", span, "degrés", None, limits.max_osm_bbox_degrees, REFUSED,
                                f"Emprise {span:.3f}° > {limits.max_osm_bbox_degrees:g}° : requête Overpass unique "
                                "impossible ; découpage en tuiles non disponible."))
        else:
            checks.append(Check("osm_bbox", "Emprise OSM", span, "degrés", None, limits.max_osm_bbox_degrees, OK,
                                "Emprise compatible avec une requête Overpass bornée."))
        checks.append(_threshold("osm_bytes", "Volume OSM estimé", None if osm_estimated_bytes is None
                                 else osm_estimated_bytes / MIB, "Mio", limits.warn_osm_bytes / MIB,
                                 limits.max_osm_bytes / MIB, "Volume OSM"))
    return ResourceAssessment(tuple(checks), limits.fingerprint)


def assess_import(path, *, limits: ResourceLimits | None = None) -> ResourceAssessment:
    """GeoJSON import constraints: size is checked **before** parsing; then strict validation, vertices, parts."""
    from src.geo.aoi_identity import load_aoi_identity

    limits = limits or ResourceLimits.from_environment()
    path = Path(path)
    if not path.is_file():
        raise ValueError("Fichier d'import introuvable.")
    size = path.stat().st_size
    size_check = _threshold("import_bytes", "Taille du fichier", size / MIB, "Mio", limits.max_import_bytes / MIB,
                            limits.max_import_bytes / MIB, "Taille")
    if size_check.status == REFUSED:
        return ResourceAssessment((size_check,), limits.fingerprint)   # Never parse an oversized file.
    identity = load_aoi_identity(path)                                  # Strict: invalid geometry raises.
    checks = (size_check,
              _threshold("import_vertices", "Sommets", identity.vertices, "sommets", limits.max_import_vertices,
                         limits.max_import_vertices, "Nombre de sommets"),
              _threshold("import_parts", "Parties", identity.parts, "parties", limits.max_import_parts,
                         limits.max_import_parts, "Nombre de parties"))
    return ResourceAssessment(checks, limits.fingerprint)
