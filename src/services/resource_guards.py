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
from pathlib import Path, PurePosixPath

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
PROC_SELF_CGROUP = Path("/proc/self/cgroup")
PROC_SELF_MOUNTINFO = Path("/proc/self/mountinfo")
CGROUP_FILES = {2: ("memory.max", "memory.current", "memory.stat", "inactive_file"),
                1: ("memory.limit_in_bytes", "memory.usage_in_bytes", "memory.stat", "total_inactive_file")}
CGROUP_UNLIMITED = 1 << 60     # cgroup v1 writes a huge page-aligned number when no limit is set.

LIMITED, UNLIMITED, ABSENT, INACCESSIBLE = "limited", "unlimited", "absent", "inaccessible"


@dataclass(frozen=True)
class CgroupMemory:
    """Effective cgroup memory figure of this process.

    ``status``: ``limited`` (a numeric limit applies), ``unlimited`` (cgroup files readable, no limit
    at any level), ``absent`` (no cgroup memory controller visible: Windows, macOS, bare process),
    ``inaccessible`` (files present but unreadable or malformed: no figure is trusted).
    ``available_bytes`` = limit − (usage − inactive file cache), the smallest over the process cgroup
    and every visible ancestor; ``level`` is the cgroup path giving it (never a host file path).
    """

    status: str
    available_bytes: int | None = None
    limit_bytes: int | None = None
    version: int | None = None
    level: str | None = None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None


def _process_cgroups(proc_cgroup: Path) -> dict[int, str]:
    """{version: cgroup path} from /proc/self/cgroup (v2 ``0::/path``, v1 line naming ``memory``)."""
    text = _read_text(proc_cgroup)
    paths: dict[int, str] = {}
    for line in (text or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, path = parts
        if hierarchy == "0" and controllers == "":
            paths[2] = path
        elif "memory" in controllers.split(","):
            paths[1] = path
    return paths


def _mount_roots(mountinfo: Path) -> dict[int, tuple[str, str]]:
    """{version: (root inside the hierarchy, mount point)} of the cgroup memory mounts."""
    text = _read_text(mountinfo)
    mounts: dict[int, tuple[str, str]] = {}
    for line in (text or "").splitlines():
        left, separator, right = line.partition(" - ")
        fields_left, fields_right = left.split(), right.split()
        if not separator or len(fields_left) < 5 or len(fields_right) < 3:
            continue
        root, mount_point, fstype, options = fields_left[3], fields_left[4], fields_right[0], fields_right[2]
        if fstype == "cgroup2" and 2 not in mounts:
            mounts[2] = (root, mount_point)
        elif fstype == "cgroup" and "memory" in options.split(",") and 1 not in mounts:
            mounts[1] = (root, mount_point)
    return mounts


def _levels(base: Path, cgroup_path: str | None, mount_root: str) -> list[tuple[Path, str]]:
    """(directory, cgroup path) from the process cgroup up to the visible root of the hierarchy."""
    if cgroup_path is None:
        return [(base, "/")]
    relative = PurePosixPath(cgroup_path)
    root = PurePosixPath(mount_root or "/")
    try:
        relative = relative.relative_to(root)          # Mounted below the hierarchy root (container).
    except ValueError:
        relative = PurePosixPath(*relative.parts[1:]) if relative.is_absolute() else relative
    parts = [part for part in relative.parts if part not in ("", "/", ".", "..")]
    levels = []
    for depth in range(len(parts), -1, -1):
        levels.append((base.joinpath(*parts[:depth]), "/" + "/".join(parts[:depth])))
    return levels


def _level_figure(directory: Path, version: int):
    """(status, available, limit) of one cgroup directory."""
    limit_name, usage_name, stat_name, inactive_key = CGROUP_FILES[version]
    limit_text = _read_text(directory / limit_name)
    if limit_text is None:
        return ABSENT, None, None
    limit_text = limit_text.strip()
    if limit_text == "max":
        return UNLIMITED, None, None
    if not limit_text.isdigit():
        return INACCESSIBLE, None, None
    limit = int(limit_text)
    if limit >= CGROUP_UNLIMITED:
        return UNLIMITED, None, None
    usage_text = (_read_text(directory / usage_name) or "").strip()
    if not usage_text.isdigit():
        return INACCESSIBLE, None, None
    inactive = 0
    for line in (_read_text(directory / stat_name) or "").splitlines():
        name, _, value = line.partition(" ")
        if name == inactive_key and value.strip().isdigit():
            inactive = int(value)
    return LIMITED, max(0, limit - max(0, int(usage_text) - inactive)), limit


def cgroup_memory(root: Path | None = None, *, proc_cgroup: Path | None = None,
                  mountinfo: Path | None = None) -> CgroupMemory:
    """Effective memory limit of this process under cgroup v2, else v1 (see :class:`CgroupMemory`).

    The process cgroup comes from ``/proc/self/cgroup``; ``/proc/self/mountinfo`` gives the part of
    the hierarchy visible under ``root`` (a container usually sees its own cgroup as the root).
    Every level from the process cgroup up to that visible root is read, so a limit set on a
    parent (systemd slice, pod) applies. With an explicit ``root`` and no /proc files, ``root`` is
    the process cgroup itself (single level). The host's total memory is never assumed.
    """
    base = CGROUP_ROOT if root is None else Path(root)
    explicit_root = root is not None and proc_cgroup is None and mountinfo is None
    cgroups = {} if explicit_root else _process_cgroups(PROC_SELF_CGROUP if proc_cgroup is None else proc_cgroup)
    mounts = {} if explicit_root else _mount_roots(PROC_SELF_MOUNTINFO if mountinfo is None else mountinfo)
    statuses = []
    for version, hierarchy in ((2, base), (1, base / "memory")):
        mount_root = mounts.get(version, ("/", ""))[0]
        figures = []
        for directory, path in _levels(hierarchy, cgroups.get(version), mount_root):
            status, available, limit = _level_figure(directory, version)
            statuses.append(status)
            if status == LIMITED:
                figures.append((available, limit, path))
            elif status == INACCESSIBLE:
                return CgroupMemory(INACCESSIBLE, version=version, level=path)
        if figures:
            available, limit, path = min(figures)
            return CgroupMemory(LIMITED, available, limit, version, path)
        if UNLIMITED in statuses:
            return CgroupMemory(UNLIMITED, version=version)
    return CgroupMemory(ABSENT)


def cgroup_memory_bytes(root: Path | None = None) -> int | None:
    """Memory left under the effective cgroup limit; None when unlimited, absent or inaccessible.

    Usage minus inactive file cache (reclaimable), as container runtimes count the working set;
    never below zero. See :func:`cgroup_memory`.
    """
    figure = cgroup_memory(root)
    return figure.available_bytes if figure.status == LIMITED else None


def available_memory_bytes() -> int | None:
    """Free memory: the smaller of the host's and of the container's cgroup limit, None when both are unreadable.

    In a container the host figure ignores the cgroup limit; without any figure the configured ceilings apply
    alone. When cgroup files exist but cannot be read, the container limit is unknown: the host figure is
    not trusted either (it may be far above what the process may use) and None is returned.
    """
    container = cgroup_memory()
    if container.status == INACCESSIBLE:
        return None
    host = None
    try:
        import psutil

        host = int(psutil.virtual_memory().available)
    except (ImportError, OSError):
        pass
    limited = container.available_bytes if container.status == LIMITED else None
    figures = [value for value in (host, limited) if value is not None]
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
