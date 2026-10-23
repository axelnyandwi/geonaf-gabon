"""Study-zone identity: strict validation and a normalized geometry fingerprint.

The file digest (``aoi_sha256``, used by existing caches) changes with any byte
of the file: indentation, property names, ring start. The geometry
fingerprint identifies the **area** instead, so that layers retrieved for one
geometry are never reused for another one under the same identifier:

1. every feature must be a valid, non-empty Polygon/MultiPolygon with a CRS
   and finite WGS84 coordinates — invalid input is refused, never repaired;
2. features are reprojected to EPSG:4326 and dissolved (``union_all``);
3. the union is put in canonical form (``shapely.normalize``: ring start and
   orientation, part order) and coordinates are rounded to 1e-7° (≈ 1 cm);
4. the fingerprint is SHA-256 of a version tag and the WKB of that geometry.

Properties never enter the fingerprint. The same area stored in another CRS
gives the same fingerprint only if reprojection agrees to 1e-7°.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import numpy as np

GEOMETRY_IDENTITY_VERSION = "geonaf-aoi-geometry-v1"
COORDINATE_DECIMALS = 7


@dataclass(frozen=True)
class AOIIdentity:
    aoi_id: str
    geometry_sha256: str                       # Normalized geometry (this module).
    file_sha256: str                           # Raw file bytes (existing caches).
    wgs84_bounds: tuple[float, float, float, float]
    geometry_type: str                         # Polygon or MultiPolygon after union.
    parts: int
    vertices: int

    @property
    def short(self) -> str:
        return self.geometry_sha256[:12]


def validate_polygonal_frame(frame) -> None:
    """Refuse (ValueError) anything but valid, non-empty polygons with a CRS; no repair."""
    from shapely import get_coordinates
    from shapely.validation import explain_validity

    if frame.crs is None:
        raise ValueError("Zone d'étude sans système de coordonnées.")
    if frame.empty:
        raise ValueError("Zone d'étude vide.")
    for index, geometry in enumerate(frame.geometry):
        if geometry is None or geometry.is_empty:
            raise ValueError(f"Géométrie vide (élément {index}).")
        if geometry.geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"Géométrie non surfacique (élément {index} : {geometry.geom_type}).")
        if not geometry.is_valid:
            raise ValueError(f"Géométrie invalide (élément {index}) : {explain_validity(geometry)}.")
    wgs84 = frame.to_crs("EPSG:4326")
    coordinates = get_coordinates(wgs84.geometry.array)
    if not np.isfinite(coordinates).all():
        raise ValueError("Coordonnées non finies après reprojection en WGS84.")
    if (np.abs(coordinates[:, 0]) > 180).any() or (np.abs(coordinates[:, 1]) > 90).any():
        raise ValueError("Coordonnées hors de la plage WGS84.")


def geometry_fingerprint(geometry_wgs84) -> str:
    """Fingerprint of an already validated EPSG:4326 (multi)polygon."""
    import shapely

    canonical = shapely.normalize(geometry_wgs84)
    rounded = shapely.transform(canonical, lambda xy: np.round(xy, COORDINATE_DECIMALS))
    digest = hashlib.sha256(GEOMETRY_IDENTITY_VERSION.encode("ascii") + b"\0")
    digest.update(shapely.to_wkb(rounded, byte_order=1, include_srid=False))
    return digest.hexdigest()


def load_aoi_identity(path, aoi_id: str | None = None) -> AOIIdentity:
    import geopandas as gpd
    from shapely import get_num_coordinates

    path = Path(path)
    raw = path.read_bytes()
    frame = gpd.read_file(path)
    validate_polygonal_frame(frame)
    union = frame.to_crs("EPSG:4326").geometry.union_all()
    if union.is_empty or union.area <= 0 or union.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("Union des géométries de la zone d'étude non surfacique.")
    return AOIIdentity(
        aoi_id or path.stem, geometry_fingerprint(union), hashlib.sha256(raw).hexdigest(),
        tuple(float(v) for v in union.bounds), union.geom_type,
        len(union.geoms) if union.geom_type == "MultiPolygon" else 1, int(get_num_coordinates(union)))
