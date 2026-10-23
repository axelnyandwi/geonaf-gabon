"""Compare two representations of one territory (e.g. gbOpen vs gbHumanitarian); no UI, no network.

A comparison measures how far two **descriptions** of limits disagree. It is
never a change of the territory: both may describe the same ground at
different vintages, scales or with different methods.

Method: both geometries are validated strictly (no repair), their edges are
densified in WGS84 (≤ 0.001°, ≈ 110 m) so that straight lon/lat edges keep
their shape once projected, then everything is computed in the World
Cylindrical Equal Area projection (EPSG:6933), the equal-area CRS already
used by GEONAF's exact area weights. Areas are therefore true areas on the
WGS84 ellipsoid up to the densification error; shapes are distorted, which
does not affect areas.
"""

from __future__ import annotations

from dataclasses import dataclass

EQUAL_AREA_CRS = "EPSG:6933"
DENSIFY_DEGREES = 0.001
METHOD = ("Géométries validées sans réparation, arêtes densifiées à 0,001° en WGS84, surfaces calculées en "
          "projection équivalente EPSG:6933 (aires vraies sur l'ellipsoïde WGS84 à la densification près).")
NATURE = ("Écart entre deux représentations des limites d'un territoire : ce n'est pas un changement du "
          "territoire réel.")


@dataclass(frozen=True)
class GeometryComparison:
    label_a: str
    label_b: str
    area_a_km2: float
    area_b_km2: float
    intersection_km2: float
    union_km2: float
    symmetric_difference_km2: float
    overlap_percent: float               # Intersection / union (Jaccard), 100 = identical.
    a_covered_by_b_percent: float        # Intersection / area A.
    b_covered_by_a_percent: float        # Intersection / area B.
    relative_area_difference_percent: float   # (area B − area A) / area A.
    method: str = METHOD
    nature: str = NATURE
    notes: tuple[str, ...] = ()


def _validated(geometry, label):
    from shapely.validation import explain_validity

    if geometry is None or geometry.is_empty:
        raise ValueError(f"{label} : géométrie vide.")
    if geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"{label} : géométrie non surfacique ({geometry.geom_type}).")
    if not geometry.is_valid:
        raise ValueError(f"{label} : géométrie invalide ({explain_validity(geometry)}).")
    return geometry


def _equal_area(geometry):
    import geopandas as gpd
    import shapely

    densified = shapely.segmentize(geometry, DENSIFY_DEGREES)
    projected = gpd.GeoSeries([densified], crs="EPSG:4326").to_crs(EQUAL_AREA_CRS).iloc[0]
    if not projected.is_valid:
        raise ValueError("Géométrie invalide après projection équivalente.")
    return projected


def compare_geometries(geometry_a, geometry_b, *, label_a: str = "A", label_b: str = "B",
                       notes=()) -> GeometryComparison:
    """Compare two EPSG:4326 (multi)polygons; areas in km²."""
    a = _equal_area(_validated(geometry_a, label_a))
    b = _equal_area(_validated(geometry_b, label_b))
    area_a, area_b = a.area / 1e6, b.area / 1e6
    intersection = a.intersection(b).area / 1e6
    union = a.union(b).area / 1e6
    symmetric = a.symmetric_difference(b).area / 1e6
    return GeometryComparison(
        label_a, label_b, area_a, area_b, intersection, union, symmetric,
        100.0 * intersection / union if union else 0.0, 100.0 * intersection / area_a, 100.0 * intersection / area_b,
        100.0 * (area_b - area_a) / area_a, notes=tuple(notes))


def compare_territories(registry, territory_a: str, territory_b: str, *, version_a: int | None = None,
                        version_b: int | None = None) -> GeometryComparison:
    """Compare two registered territories; the records are only read, never merged or modified."""
    import geopandas as gpd

    a, b = registry.get(territory_a, version_a), registry.get(territory_b, version_b)
    if (a.territory_id, a.version) == (b.territory_id, b.version):
        raise ValueError("Comparaison d'une version avec elle-même.")
    geometries = [gpd.read_file(registry.geometry_path(r)).to_crs("EPSG:4326").geometry.union_all() for r in (a, b)]
    notes = [f"{r.label} : {r.source.product} {r.source.version}, statut « {r.legal_status} »"
             + (f", limites de {r.source.reference_year}" if r.source.reference_year else "") for r in (a, b)]
    if a.source.reference_year != b.source.reference_year:
        notes.append("Millésimes différents : l'écart ne mesure pas une évolution des limites dans le temps.")
    if a.territory_id == b.territory_id:
        notes.append("Deux versions d'un même territoire : nouvelle description des limites, pas un changement réel.")
    return compare_geometries(*geometries, label_a=f"{a.territory_id} v{a.version}",
                              label_b=f"{b.territory_id} v{b.version}", notes=notes)


def match_by_normalized_name(records_a, records_b) -> tuple[tuple[object, object], ...]:
    """Candidate pairs sharing the same normalized name (accents and case ignored); to be checked, never merged."""
    from src.data.geoboundaries import slug

    index = {}
    for record in records_b:
        index.setdefault(slug(record.label), []).append(record)
    return tuple((record, index[slug(record.label)][0]) for record in records_a
                 if len(index.get(slug(record.label), [])) == 1)
